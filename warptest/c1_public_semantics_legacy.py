"""Legacy root-format Love/SpaceRPG public-semantic compatibility.

Formal C1 evaluation-v1 specs never enter this module.  It exists only so
non-C1 root-format specs keep their historical code-shaped semantics while the
formal evaluator remains entirely contract-driven.
"""
from __future__ import annotations

import re
from typing import Any

from .c1_public_semantics import (
    _check,
    _check_greater,
    _check_gte,
    _checks_ok,
    _spacerpg_severe_page_errors,
    _numeric_delta,
    _parse_int,
    _path_get,
    _real_api_attempts,
    _runtime_error_summary,
    _runtime_network_summary,
)

_SPACERPG_SAVE_KEYS = {
    "auto": "spacerpg-save-slot:auto",
    "manual-1": "spacerpg-save-slot:manual-1",
}

def _grade_love_start_legacy(
    snapshot: dict[str, Any], oracle_spec: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    state = snapshot["state"]
    dom = snapshot["dom"]
    target = oracle_spec.get("target") if isinstance(oracle_spec.get("target"), dict) else {}
    expected = target.get("expect") if isinstance(target.get("expect"), dict) else {}
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "probe.semantic_observation_error",
        snapshot.get("semantic_observation_error"),
        None,
    )
    _check(checks, "screen.gameplay", dom.get("screen"), "gameplay")
    _check(checks, "state.isSetupComplete", state.get("isSetupComplete"), True)
    _check(checks, "state.player.name", _path_get(state, "player.name"), target.get("player_name", "WarpTest"))
    _check(checks, "state.player.gender", _path_get(state, "player.gender"), target.get("gender", "male"))
    _check(checks, "dom.player_name", dom.get("player_name"), target.get("player_name", "WarpTest"))
    for surface in ("app_visible", "dashboard_visible", "location_panel_visible", "action_grid_visible"):
        _check(checks, f"dom.{surface}", dom.get(surface), True)
    resources = expected.get("resources") if isinstance(expected.get("resources"), dict) else {}
    for key, value in resources.items():
        _check(checks, f"state.player.resources.{key}", _path_get(state, f"player.resources.{key}"), value)
    expected_location = expected.get("location") or _path_get(target, "patch.world.location") or "home"
    _check(checks, "state.world.location", _path_get(state, "world.location"), expected_location)
    location_index = {
        "home": 0,
        "office": 1,
        "gym": 2,
        "cafe": 3,
        "restaurant": 4,
        "shopping_mall": 5,
        "bar": 6,
        "park": 7,
    }.get(str(expected_location))
    if location_index is not None:
        _check(checks, "dom.active_location_index", dom.get("active_location_index"), location_index)
    action_index = _love_action_index(oracle_spec)
    action_buttons = dom.get("action_buttons") if isinstance(dom.get("action_buttons"), list) else []
    action_button = action_buttons[action_index] if action_index is not None and action_index < len(action_buttons) else None
    if action_index is not None:
        _check(checks, "dom.target_action_present", isinstance(action_button, dict), True)
    if action_index is not None and isinstance(action_button, dict):
        _check(checks, "dom.target_action_enabled", action_button.get("disabled"), False)
        _check(checks, "dom.target_action_visible", action_button.get("visible"), True)
    _check(checks, "environment.ai_query_mode", dom.get("ai_query_mode"), "mocked")
    # A/C must not use the storage toggle because that toggle also enables the state hook.
    # B/D restore pages legitimately carry it; the semantic source remains public in both.
    if dom.get("ai_query_mode") == "mocked" and dom.get("ai_storage_mode") is None:
        _check(checks, "environment.ai_storage_mode", None, None)
    _check(checks, "probe.prior_restored", snapshot.get("prior_restored"), True)
    return _checks_ok(checks), checks

def _grade_love_goal_legacy(
    snapshot: dict[str, Any],
    baseline: dict[str, Any],
    oracle_spec: dict[str, Any],
    runtime: Any,
) -> tuple[bool, list[dict[str, Any]]]:
    state = snapshot["state"]
    before_state = baseline["state"]
    dom = snapshot["dom"]
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "probe.semantic_observation_error",
        snapshot.get("semantic_observation_error"),
        None,
    )
    _check(checks, "screen.gameplay", dom.get("screen"), "gameplay")
    _check(checks, "state.isSetupComplete", state.get("isSetupComplete"), True)
    for assertion in oracle_spec.get("assertions", []) or []:
        kind = assertion.get("type")
        if kind == "state_equals":
            path = str(assertion.get("path", ""))
            _check(checks, f"assert.{path}", _path_get(state, path), assertion.get("value"))
        elif kind == "no_real_openai":
            attempted = _real_api_attempts(runtime)
            _check(checks, "assert.no_real_openai", attempted, [])
            _check(checks, "environment.ai_query_mode", dom.get("ai_query_mode"), "mocked")
        elif kind == "no_severe_console":
            _check(checks, "assert.no_page_errors", snapshot["errors"].get("page"), [])
            _check(
                checks,
                "assert.no_console_errors",
                snapshot["errors"].get("console"),
                [],
            )
        else:
            _check(checks, f"assert.unsupported.{kind}", kind, "supported")
    action_id = _love_action_id(oracle_spec)
    before_log = before_state.get("log") if isinstance(before_state.get("log"), list) else []
    after_log = state.get("log") if isinstance(state.get("log"), list) else []
    suffix = after_log[len(before_log) :] if after_log[: len(before_log)] == before_log else after_log
    matching = [item for item in suffix if isinstance(item, dict) and item.get("text") == f"Performed action: {action_id}"]
    _check(checks, "transition.target_action_count", len(matching), 1)
    _check(checks, "transition.new_log_entries", len(suffix), 1)
    _check(checks, "probe.prior_restored", snapshot.get("prior_restored"), True)
    return _checks_ok(checks), checks

def _grade_spacerpg_start_legacy(
    snapshot: dict[str, Any], oracle_spec: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    dom = snapshot["dom"]
    target = oracle_spec.get("target") if isinstance(oracle_spec.get("target"), dict) else {}
    expected_screen = target.get("screen")
    checks: list[dict[str, Any]] = []
    if expected_screen:
        _check(checks, "dom.screen", dom.get("screen"), expected_screen)
    _spacerpg_render_checks(checks, dom, expected_screen)
    expected_system = target.get("system_id")
    if expected_system:
        _check(checks, "dom.current_system", _spacerpg_current_system(dom), expected_system)
    expected_station = target.get("station_id")
    if expected_station:
        _check(checks, "dom.station", _spacerpg_station_id(dom), expected_station)
    expected_mode = target.get("galaxy_map_mode")
    if expected_mode == "gate":
        _check(checks, "dom.galaxy_mode", _spacerpg_gate_mode(dom), True)
    if _is_spacerpg_trade(oracle_spec):
        _check(checks, "dom.station_active_tab", _normalized_tab(dom.get("station_active_tab")), "market")
        _check(checks, "dom.basic_food", _spacerpg_basic_food(dom), 3)
        _check(checks, "dom.market_sell_enabled", dom.get("market_sell_enabled"), True)
    if _is_spacerpg_manual_restore(oracle_spec):
        manual = snapshot["storage"].get("manual_1", {})
        _check(checks, "storage.manual_1_absent", manual.get("exists"), False)
    _check(checks, "environment.no_real_api", snapshot["network"].get("real_api_attempts"), [])
    return _checks_ok(checks), checks

def _grade_spacerpg_goal_legacy(
    snapshot: dict[str, Any],
    baseline: dict[str, Any],
    oracle_spec: dict[str, Any],
    trace: dict[str, Any],
    runtime: Any,
) -> tuple[bool, list[dict[str, Any]]]:
    dom = snapshot["dom"]
    before_dom = baseline["dom"]
    checks: list[dict[str, Any]] = []
    for assertion in oracle_spec.get("assertions", []) or []:
        kind = assertion.get("type")
        path = str(assertion.get("path", ""))
        if kind == "state_equals":
            actual = _spacerpg_public_path(dom, snapshot, path)
            _check(checks, f"assert.{path}", actual, assertion.get("value"))
        elif kind == "state_contains":
            expected = assertion.get("value")
            values = _spacerpg_public_collection(dom, path)
            _check(checks, f"assert.{path}.contains", expected in values, True)
        elif kind == "state_gte":
            actual = _spacerpg_public_path(dom, snapshot, path)
            _check_gte(checks, f"assert.{path}", actual, assertion.get("value"))
        elif kind == "localStorage_key_exists":
            slot = _spacerpg_slot_for_key(str(assertion.get("key", "")))
            entry = snapshot["storage"].get(slot, {}) if slot else {}
            _check(checks, f"assert.storage.{slot}.exists", entry.get("exists"), True)
            _check(checks, f"assert.storage.{slot}.parse_ok", entry.get("parse_ok"), True)
        elif kind == "no_severe_console":
            _check(
                checks,
                "assert.no_page_errors",
                _spacerpg_severe_page_errors(snapshot["errors"].get("page")),
                [],
            )
            _check(checks, "assert.no_console_errors", snapshot["errors"].get("console"), [])
        else:
            _check(checks, f"assert.unsupported.{kind}", kind, "supported")

    target_screen = _asserted_value(oracle_spec, "screen")
    _spacerpg_render_checks(checks, dom, target_screen)
    if _is_spacerpg_flight(oracle_spec):
        before_throttle = _spacerpg_throttle(before_dom)
        after_throttle = _spacerpg_throttle(dom)
        _check_greater(checks, "transition.throttle_delta", after_throttle, before_throttle)
        _check(checks, "transition.throttle_intent_count", _count_throttle_intents(trace), 1)
    if _is_spacerpg_galaxy_jump(oracle_spec):
        screens = _trace_screens(trace)
        _check(checks, "transition.screen_trace_start", screens[0] if screens else None, "galaxyMap")
        _check(checks, "transition.screen_trace_end", screens[-1] if screens else None, "flight")
    if _is_spacerpg_auto_save(oracle_spec):
        before_auto = baseline["storage"].get("auto", {})
        after_auto = snapshot["storage"].get("auto", {})
        _check(checks, "transition.auto_fingerprint_changed", after_auto.get("fingerprint") != before_auto.get("fingerprint"), True)
        _check(checks, "transition.save_intent", _has_save_intent(trace, slot="auto"), True)
        _check_spacerpg_save_payload(checks, after_auto, slot="auto")
        _check(checks, "storage.index.last_played", _spacerpg_last_played(snapshot), "auto")
    if _is_spacerpg_manual_restore(oracle_spec):
        manual = snapshot["storage"].get("manual_1", {})
        _check_spacerpg_save_payload(checks, manual, slot="manual-1")
        _check(checks, "storage.index.last_played", _spacerpg_last_played(snapshot), "manual-1")
        screens = _trace_screens(trace)
        _check(checks, "transition.station_flight_station", _contains_ordered(screens, ["station", "flight", "station"]), True)
        _check(checks, "transition.manual_save_intent", _has_save_intent(trace, slot="manual-1"), True)
        _check(checks, "transition.manual_load_intent", _has_load_intent(trace, slot="manual-1"), True)
    if _is_spacerpg_trade(oracle_spec):
        before_qty = _spacerpg_basic_food(before_dom)
        after_qty = _spacerpg_basic_food(dom)
        before_credits = _spacerpg_credits(before_dom)
        after_credits = _spacerpg_credits(dom)
        _check(checks, "transition.basic_food_delta", _numeric_delta(before_qty, after_qty), 1)
        _check_greater(checks, "transition.credits_increased", after_credits, before_credits)
        _check(checks, "transition.basic_food_sell_intent", _has_basic_food_sell_intent(trace), True)
    _check(checks, "environment.no_real_api", _real_api_attempts(runtime), [])
    _check(checks, "trace.errors", trace.get("errors", []), [])
    return _checks_ok(checks), checks

def _spacerpg_render_checks(checks: list[dict[str, Any]], dom: dict[str, Any], screen: Any) -> None:
    _check(checks, "dom.fatal_overlay", dom.get("fatal_overlay"), False)
    _check(checks, "dom.blocking_overlay", dom.get("blocking_overlay"), False)
    _check_gte(checks, "dom.actionable_buttons", dom.get("actionable_buttons"), 1)
    if screen == "flight":
        canvas = dom.get("canvas") if isinstance(dom.get("canvas"), dict) else {}
        _check(checks, "dom.canvas.visible", canvas.get("visible"), True)
        _check_gte(checks, "dom.canvas.css_width", canvas.get("css_width"), 33)
        _check_gte(checks, "dom.canvas.css_height", canvas.get("css_height"), 33)
        _check_gte(checks, "dom.canvas.bitmap_width", canvas.get("bitmap_width"), 33)
        _check_gte(checks, "dom.canvas.bitmap_height", canvas.get("bitmap_height"), 33)

def _check_spacerpg_save_payload(checks: list[dict[str, Any]], entry: dict[str, Any], *, slot: str) -> None:
    value = entry.get("value") if isinstance(entry.get("value"), dict) else {}
    _check(checks, f"storage.{slot}.exists", entry.get("exists"), True)
    _check(checks, f"storage.{slot}.parse_ok", entry.get("parse_ok"), True)
    _check(checks, f"storage.{slot}.version", value.get("version"), 3)
    _check(checks, f"storage.{slot}.screen", value.get("screen"), "station")
    _check(checks, f"storage.{slot}.system", value.get("current_system_id"), "helion-reach")
    _check(checks, f"storage.{slot}.station", value.get("current_station_id"), "helion-prime")

def _spacerpg_public_path(dom: dict[str, Any], snapshot: dict[str, Any], path: str) -> Any:
    if path == "screen":
        return dom.get("screen")
    if path == "currentSystemId":
        return _spacerpg_current_system(dom)
    if path == "activeSaveSlotId":
        return _spacerpg_last_played(snapshot)
    if path == "player.cargo.basic-food":
        return _spacerpg_basic_food(dom)
    if path == "player.credits":
        return _spacerpg_credits(dom)
    return None

def _spacerpg_public_collection(dom: dict[str, Any], path: str) -> list[Any]:
    if path == "knownSystems":
        values = list(dom.get("galaxy_known_system_ids") or [])
        current = _spacerpg_current_system(dom)
        if current and current not in values:
            values.append(current)
        return values
    return []

def _spacerpg_current_system(dom: dict[str, Any]) -> str | None:
    current = dom.get("galaxy_current_system_id")
    if isinstance(current, str) and current:
        return current
    text = str(dom.get("hud_system_text") or "").lower()
    if "helion reach" in text or "赫利昂星域" in text:
        return "helion-reach"
    if "kuro belt" in text or "黑带" in text:
        return "kuro-belt"
    if _spacerpg_station_id(dom) == "helion-prime":
        return "helion-reach"
    return None

def _spacerpg_station_id(dom: dict[str, Any]) -> str | None:
    text = str(dom.get("station_heading") or "").lower()
    if "helion prime" in text or "赫利昂主星" in text:
        return "helion-prime"
    return None

def _spacerpg_gate_mode(dom: dict[str, Any]) -> bool:
    text = str(dom.get("galaxy_mode_text") or "").lower()
    return any(label in text for label in ("stargate link", "星门", "スターゲート", "porte stellaire"))

def _spacerpg_throttle(dom: dict[str, Any]) -> int | None:
    text = str(dom.get("hud_throttle_text") or "")
    match = re.search(r"(?:Throttle|节流|スロットル|Accélérateur)\s*([0-9][0-9,\s]*)%", text, re.IGNORECASE)
    return _parse_int(match.group(1)) if match else None

def _spacerpg_basic_food(dom: dict[str, Any]) -> int | None:
    text = str(dom.get("market_row_text") or "")
    patterns = (
        r"(?:Hold|持有|保有|Soute)\s*([0-9][0-9,\s]*)",
        r"Basic Food.*?([0-9][0-9,\s]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
    return None

def _spacerpg_credits(dom: dict[str, Any]) -> int | None:
    text = str(dom.get("market_panel_text") or "")
    patterns = (
        r"([0-9][0-9,\s]*)\s*(?:credits|cr|信用点|信用點|クレジット|crédits)",
        r"(?:credits|信用点|信用點|クレジット|crédits)\s*([0-9][0-9,\s]*)",
    )
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
    return None

def _normalized_tab(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if text in {"market", "市场", "市場", "マーケット", "marché"}:
        return "market"
    return text or None

def _love_action_id(oracle_spec: dict[str, Any]) -> str | None:
    for action in oracle_spec.get("actions", []) or []:
        if action.get("type") == "hook_action":
            return str(action.get("action_id"))
    return None

def _love_action_index(oracle_spec: dict[str, Any]) -> int | None:
    return {
        "work_coding": 0,
        "gym_workout": 1,
        "spa_visit": 2,
        "date_coffee": 3,
    }.get(_love_action_id(oracle_spec))

def _spacerpg_action(oracle_spec: dict[str, Any], method: str) -> dict[str, Any] | None:
    for action in oracle_spec.get("actions", []) or []:
        if action.get("type") == "hook_call" and action.get("method") == method:
            return action
    return None

def _is_spacerpg_flight(oracle_spec: dict[str, Any]) -> bool:
    return _spacerpg_action(oracle_spec, "jumpToSystem") is None and _asserted_value(oracle_spec, "screen") == "flight" and any(
        action.get("type") == "key" for action in oracle_spec.get("actions", []) or []
    )

def _is_spacerpg_galaxy_jump(oracle_spec: dict[str, Any]) -> bool:
    return _spacerpg_action(oracle_spec, "jumpToSystem") is not None

def _is_spacerpg_auto_save(oracle_spec: dict[str, Any]) -> bool:
    action = _spacerpg_action(oracle_spec, "saveGame")
    return bool(action and (action.get("args") or [None])[0] == "auto")

def _is_spacerpg_manual_restore(oracle_spec: dict[str, Any]) -> bool:
    action = _spacerpg_action(oracle_spec, "saveGame")
    return bool(action and (action.get("args") or [None])[0] == "manual-1" and _spacerpg_action(oracle_spec, "loadGame"))

def _is_spacerpg_trade(oracle_spec: dict[str, Any]) -> bool:
    return _spacerpg_action(oracle_spec, "sell") is not None

def _asserted_value(oracle_spec: dict[str, Any], path: str) -> Any:
    for assertion in oracle_spec.get("assertions", []) or []:
        if assertion.get("type") == "state_equals" and assertion.get("path") == path:
            return assertion.get("value")
    return None

def _spacerpg_slot_for_key(key: str) -> str | None:
    for slot, storage_key in _SPACERPG_SAVE_KEYS.items():
        if key == storage_key:
            return "manual_1" if slot == "manual-1" else slot
    return None

def _spacerpg_last_played(snapshot: dict[str, Any]) -> Any:
    index = snapshot.get("storage", {}).get("index", {})
    value = index.get("value") if isinstance(index.get("value"), dict) else {}
    return value.get("last_played_slot")

def _trace_screens(trace: dict[str, Any]) -> list[str]:
    screens: list[str] = []
    baseline = trace.get("baseline")
    if isinstance(baseline, dict):
        screen = (baseline.get("dom") or {}).get("screen")
        if isinstance(screen, str):
            screens.append(screen)
    for event in trace.get("events", []) or []:
        screen = (event.get("dom") or {}).get("screen") if isinstance(event, dict) else None
        if isinstance(screen, str) and (not screens or screens[-1] != screen):
            screens.append(screen)
    return screens

def _target_action_text(event: dict[str, Any]) -> str:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    return " ".join(
        str(target.get(key) or "")
        for key in ("text", "title", "aria_label")
    ).lower()

def _target_context_text(event: dict[str, Any]) -> str:
    target = event.get("target") if isinstance(event.get("target"), dict) else {}
    return " ".join(
        str(target.get(key) or "")
        for key in ("context_text", "context_test_id")
    ).lower()

def _before_events(trace: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in trace.get("events", []) or []
        if isinstance(event, dict) and event.get("when") == "before"
    ]

def _has_save_intent(trace: dict[str, Any], *, slot: str) -> bool:
    for event in _before_events(trace):
        key = _trace_key(event)
        action_text = _target_action_text(event)
        context_text = _target_context_text(event)
        if slot == "auto" and (
            key in {"ctrl+s", "control+s", "meta+s"}
            or "quick save" in action_text
        ):
            return True
        if (
            slot == "manual-1"
            and (
                "manual slot 1" in context_text
                or "手动存档 1" in context_text
            )
            and re.search(r"\bsave\b|保存", action_text)
        ):
            return True
    return False

def _has_load_intent(trace: dict[str, Any], *, slot: str) -> bool:
    for event in _before_events(trace):
        key = _trace_key(event)
        action_text = _target_action_text(event)
        context_text = _target_context_text(event)
        if slot == "manual-1" and key in {"ctrl+r", "control+r", "meta+r"}:
            return True
        if (
            slot == "manual-1"
            and (
                "manual slot 1" in context_text
                or "手动存档 1" in context_text
            )
            and re.search(r"\bload\b|加载|讀取", action_text)
        ):
            return True
    return False

def _has_basic_food_sell_intent(trace: dict[str, Any]) -> bool:
    for event in _before_events(trace):
        action_text = _target_action_text(event)
        context_text = _target_context_text(event)
        if (
            "market-row-basic-food" in context_text
            or "basic food" in context_text
            or "基础食品" in context_text
        ) and re.search(r"\bsell\b|出售|賣出", action_text):
            return True
    return False

def _count_throttle_intents(trace: dict[str, Any]) -> int:
    count = 0
    for event in _before_events(trace):
        key = str(event.get("key") or "").strip().lower()
        text = _target_action_text(event)
        if key == "w" or "throttle" in text:
            count += 1
    return count

def _trace_key(event: dict[str, Any]) -> str:
    key = str(event.get("key") or "").lower().replace(" ", "")
    modifiers = [
        str(item).lower().replace("control", "ctrl")
        for item in (event.get("modifiers") or [])
    ]
    if modifiers and "+" not in key:
        key = "+".join([*modifiers, key])
    return key.replace("control", "ctrl")

def _contains_ordered(values: list[Any], expected: list[Any]) -> bool:
    index = 0
    for value in values:
        if index < len(expected) and value == expected[index]:
            index += 1
    return index == len(expected)
