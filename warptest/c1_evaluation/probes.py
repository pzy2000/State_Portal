from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Callable, Mapping

if TYPE_CHECKING:
    from .engine import EvaluationContext


Probe = Callable[[Mapping[str, Any], "EvaluationContext"], Any]


def _path_get(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, Mapping):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _phase(config: Mapping[str, Any], context: "EvaluationContext") -> Any:
    phase = str(config.get("phase") or "current")
    if phase == "current":
        return context.current
    if phase == "before":
        return context.before
    if phase == "after":
        return context.after if context.after is not None else context.current
    if phase == "trace":
        return context.trace
    if phase == "spec":
        return context.spec
    if phase == "extras":
        return context.extras
    raise ValueError(f"unsupported C1 evaluation phase: {phase}")


def _parse_int(value: Any) -> int | None:
    text = re.sub(r"[^0-9-]", "", str(value or ""))
    try:
        return int(text)
    except ValueError:
        return None


def _common_path(config: Mapping[str, Any], context: "EvaluationContext") -> Any:
    return _path_get(_phase(config, context), str(config.get("path") or ""))


def _love_suffix(context: "EvaluationContext") -> list[Any]:
    before_log = _path_get(context.before or {}, "state.log")
    after_log = _path_get(context.current, "state.log")
    before = before_log if isinstance(before_log, list) else []
    after = after_log if isinstance(after_log, list) else []
    return after[len(before) :] if after[: len(before)] == before else after


def _love_target_action_count(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> int:
    action_id = config.get("action_id")
    expected_text = f"Performed action: {action_id}"
    return sum(
        1
        for item in _love_suffix(context)
        if isinstance(item, Mapping) and item.get("text") == expected_text
    )


def _love_target_action_present(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    value = _path_get(context.current, str(config.get("path") or ""))
    return isinstance(value, Mapping)


def _love_new_log_entries(
    _config: Mapping[str, Any], context: "EvaluationContext"
) -> int:
    return len(_love_suffix(context))


def _spacerpg_dom(config: Mapping[str, Any], context: "EvaluationContext") -> Mapping[str, Any]:
    value = _phase(config, context)
    if isinstance(value, Mapping):
        dom = value.get("dom")
        return dom if isinstance(dom, Mapping) else value
    return {}


def _spacerpg_active_save_value(snapshot: Mapping[str, Any], key: str) -> Any:
    storage = snapshot.get("storage")
    storage = storage if isinstance(storage, Mapping) else {}
    slot_id = _path_get(storage, "index.value.last_played_slot")
    slot = "manual_1" if slot_id == "manual-1" else "auto"
    return _path_get(storage, f"{slot}.value.{key}")


def _spacerpg_throttle_percent(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> int | None:
    text = str(_spacerpg_dom(config, context).get("hud_throttle_text") or "")
    match = re.search(
        r"(?:Throttle|节流|スロットル|Accélérateur)\s*([0-9][0-9,\s]*)%",
        text,
        re.IGNORECASE,
    )
    return _parse_int(match.group(1)) if match else None


def _spacerpg_basic_food(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> int | None:
    return _spacerpg_basic_food_from_dom(_spacerpg_dom(config, context))


def _spacerpg_basic_food_from_dom(dom: Mapping[str, Any]) -> int | None:
    text = str(dom.get("market_row_text") or "")
    for pattern in (
        r"(?:Hold|持有|保有|Soute)\s*([0-9][0-9,\s]*)",
        r"Basic Food.*?([0-9][0-9,\s]*)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
    return None


def _spacerpg_credits(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> int | None:
    value = _phase(config, context)
    dom_value = _spacerpg_credits_from_dom(_spacerpg_dom(config, context))
    if dom_value is not None:
        return dom_value
    return _spacerpg_active_save_value(value, "credits") if isinstance(value, Mapping) else None


def _spacerpg_credits_from_dom(dom: Mapping[str, Any]) -> int | None:
    text = str(dom.get("market_panel_text") or "")
    for pattern in (
        r"([0-9][0-9,\s]*)\s*(?:credits|cr|信用点|信用點|クレジット|crédits)",
        r"(?:credits|信用点|信用點|クレジット|crédits)\s*([0-9][0-9,\s]*)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
    return None


def _spacerpg_gate_mode(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    text = str(_spacerpg_dom(config, context).get("galaxy_mode_text") or "").lower()
    return any(
        label in text
        for label in ("stargate link", "星门", "スターゲート", "porte stellaire")
    )


def _spacerpg_normalized_tab(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> str | None:
    text = str(_spacerpg_dom(config, context).get("station_active_tab") or "").strip().lower()
    if text in {"market", "市场", "市場", "マーケット", "marché"}:
        return "market"
    if text in {"hangar", "机库", "機庫", "ハンガー"}:
        return "hangar"
    return text or None


def _spacerpg_station_id_from_dom(dom: Mapping[str, Any]) -> str | None:
    text = str(dom.get("station_heading") or "").lower()
    station_labels = {
        "helion-prime": (
            "helion prime",
            "赫利昂主星",
            "ヘリオン・プライム",
            "bourse d'hélion prime",
        ),
        "ashen-freeport": (
            "ashen freeport",
            "灰烬自由港",
            "灰燼自由港",
            "port libre cendré",
        ),
    }
    for station_id, labels in station_labels.items():
        if any(label in text for label in labels):
            return station_id
    return None


def _spacerpg_station_id(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> str | None:
    return _spacerpg_station_id_from_dom(_spacerpg_dom(config, context))


def _spacerpg_current_system(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> str | None:
    return _spacerpg_current_system_from_dom(_spacerpg_dom(config, context))


def _spacerpg_current_system_from_dom(dom: Mapping[str, Any]) -> str | None:
    current = dom.get("galaxy_current_system_id")
    if isinstance(current, str) and current:
        return current
    text = str(dom.get("hud_system_text") or "").lower()
    if "helion reach" in text or "赫利昂星域" in text:
        return "helion-reach"
    if "kuro belt" in text or "黑带" in text:
        return "kuro-belt"
    station_id = _spacerpg_station_id_from_dom(dom)
    if station_id == "helion-prime":
        return "helion-reach"
    if station_id == "ashen-freeport":
        return "ashen-drift"
    return None


def spacerpg_public_assertion_value(
    snapshot: Mapping[str, Any], path: str
) -> Any:
    dom = snapshot.get("dom")
    dom = dom if isinstance(dom, Mapping) else {}
    if path == "screen":
        return dom.get("screen")
    if path == "currentSystemId":
        return _spacerpg_current_system_from_dom(dom)
    if path == "currentStationId":
        station_id = _spacerpg_station_id_from_dom(dom)
        return station_id if station_id == "ashen-freeport" else None
    if path == "activeSaveSlotId":
        return _path_get(snapshot, "storage.index.value.last_played_slot")
    if path == "player.cargo.basic-food":
        return _spacerpg_basic_food_from_dom(dom)
    if path == "player.cargo.illegal-contraband":
        text = str(dom.get("hangar_cargo_text") or "")
        match = re.search(r"(?:Cargo|货舱|貨物|積載|Soute)\s*([0-9][0-9,\s]*)/", text, re.IGNORECASE)
        if match:
            return _parse_int(match.group(1))
        return _spacerpg_active_save_value(snapshot, "illegal_contraband")
    if path == "player.credits":
        dom_value = _spacerpg_credits_from_dom(dom)
        if dom_value is not None:
            return dom_value
        return _spacerpg_active_save_value(snapshot, "credits")
    return None


def spacerpg_public_assertion_collection(
    snapshot: Mapping[str, Any], path: str
) -> list[Any]:
    dom = snapshot.get("dom")
    dom = dom if isinstance(dom, Mapping) else {}
    if path == "player.equipment":
        text = " ".join(str(item) for item in dom.get("hangar_installed_equipment_text") or []).lower()
        known = {
            "shielded-holds": (
                "shielded holds",
                "屏蔽货舱",
                "遮蔽貨物庫",
                "soutes blindées",
            ),
            "decoy-transponder": (
                "decoy transponder",
                "诱饵应答器",
                "デコイトランスポンダー",
                "transpondeur leurre",
            ),
        }
        visible = [
            equipment_id
            for equipment_id, labels in known.items()
            if any(label in text for label in labels)
        ]
        if visible:
            return visible
        saved = _spacerpg_active_save_value(snapshot, "equipment")
        return list(saved) if isinstance(saved, list) else []
    if path != "knownSystems":
        return []
    values = list(dom.get("galaxy_known_system_ids") or [])
    current = _spacerpg_current_system_from_dom(dom)
    if current and current not in values:
        values.append(current)
    return values


def spacerpg_storage_slot_for_key(key: str) -> str | None:
    return {
        "spacerpg-save-slot:auto": "auto",
        "spacerpg-save-slot:manual-1": "manual_1",
    }.get(key)


def _trace_events(context: "EvaluationContext") -> list[Mapping[str, Any]]:
    trace = context.trace if isinstance(context.trace, Mapping) else {}
    return [item for item in trace.get("events", []) or [] if isinstance(item, Mapping)]


def _before_events(context: "EvaluationContext") -> list[Mapping[str, Any]]:
    return [item for item in _trace_events(context) if item.get("when") == "before"]


def _target_action_text(event: Mapping[str, Any]) -> str:
    target = event.get("target") if isinstance(event.get("target"), Mapping) else {}
    return " ".join(str(target.get(key) or "") for key in ("text", "title", "aria_label")).lower()


def _target_context_text(event: Mapping[str, Any]) -> str:
    target = event.get("target") if isinstance(event.get("target"), Mapping) else {}
    return " ".join(str(target.get(key) or "") for key in ("context_text", "context_test_id")).lower()


def _trace_key(event: Mapping[str, Any]) -> str:
    key = str(event.get("key") or "").lower().replace(" ", "")
    modifiers = [
        str(item).lower().replace("control", "ctrl")
        for item in (event.get("modifiers") or [])
    ]
    if modifiers and "+" not in key:
        key = "+".join([*modifiers, key])
    return key.replace("control", "ctrl")


def _spacerpg_throttle_intent_count(
    _config: Mapping[str, Any], context: "EvaluationContext"
) -> int:
    return sum(
        1
        for event in _before_events(context)
        if str(event.get("key") or "").strip().lower() == "w"
        or "throttle" in _target_action_text(event)
    )


def _spacerpg_save_intent(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    slot = str(config.get("slot") or "")
    for event in _before_events(context):
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
            and ("manual slot 1" in context_text or "手动存档 1" in context_text)
            and re.search(r"\bsave\b|保存", action_text)
        ):
            return True
    return False


def _spacerpg_load_intent(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    slot = str(config.get("slot") or "")
    for event in _before_events(context):
        key = _trace_key(event)
        action_text = _target_action_text(event)
        context_text = _target_context_text(event)
        if slot == "manual-1" and key in {"ctrl+r", "control+r", "meta+r"}:
            return True
        if (
            slot == "manual-1"
            and ("manual slot 1" in context_text or "手动存档 1" in context_text)
            and re.search(r"\bload\b|加载|讀取", action_text)
        ):
            return True
    return False


def _spacerpg_basic_food_sell_intent(
    _config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    for event in _before_events(context):
        action_text = _target_action_text(event)
        context_text = _target_context_text(event)
        if (
            "market-row-basic-food" in context_text
            or "basic food" in context_text
            or "基础食品" in context_text
        ) and re.search(r"\bsell\b|出售|賣出", action_text):
            return True
    return False


def _trace_screens(context: "EvaluationContext") -> list[str]:
    trace = context.trace if isinstance(context.trace, Mapping) else {}
    screens: list[str] = []
    baseline = trace.get("baseline")
    if isinstance(baseline, Mapping):
        screen = _path_get(baseline, "dom.screen")
        if isinstance(screen, str):
            screens.append(screen)
    for event in _trace_events(context):
        screen = _path_get(event, "dom.screen")
        if isinstance(screen, str) and (not screens or screens[-1] != screen):
            screens.append(screen)
    return screens


def _spacerpg_screen_trace_start(
    _config: Mapping[str, Any], context: "EvaluationContext"
) -> str | None:
    screens = _trace_screens(context)
    return screens[0] if screens else None


def _spacerpg_screen_trace_end(
    _config: Mapping[str, Any], context: "EvaluationContext"
) -> str | None:
    screens = _trace_screens(context)
    return screens[-1] if screens else None


def _spacerpg_screen_trace_ordered(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    expected = list(config.get("values") or [])
    index = 0
    for value in _trace_screens(context):
        if index < len(expected) and value == expected[index]:
            index += 1
    return index == len(expected)


def _spacerpg_save_fingerprint_changed(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> bool:
    slot = str(config.get("slot") or "")
    before = _path_get(context.before or {}, f"storage.{slot}.fingerprint")
    after = _path_get(context.current, f"storage.{slot}.fingerprint")
    return after != before


def _spacerpg_last_played_slot(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> Any:
    return _path_get(_phase(config, context), "storage.index.value.last_played_slot")


def _spacerpg_numeric_delta(
    config: Mapping[str, Any], context: "EvaluationContext"
) -> int | float | None:
    path = str(config.get("path") or "basic_food")
    probe = _spacerpg_basic_food if path == "basic_food" else _spacerpg_credits
    before = probe({"phase": "before"}, context)
    after = probe({"phase": "after"}, context)
    if not isinstance(before, (int, float)) or not isinstance(after, (int, float)):
        return None
    return before - after


PROBES: dict[str, Probe] = {
    "common.path": _common_path,
    "love.target_action_count": _love_target_action_count,
    "love.target_action_present": _love_target_action_present,
    "love.new_log_entries": _love_new_log_entries,
    "spacerpg.throttle_percent": _spacerpg_throttle_percent,
    "spacerpg.basic_food": _spacerpg_basic_food,
    "spacerpg.credits": _spacerpg_credits,
    "spacerpg.current_system": _spacerpg_current_system,
    "spacerpg.gate_mode": _spacerpg_gate_mode,
    "spacerpg.normalized_tab": _spacerpg_normalized_tab,
    "spacerpg.station_id": _spacerpg_station_id,
    "spacerpg.throttle_intent_count": _spacerpg_throttle_intent_count,
    "spacerpg.save_intent": _spacerpg_save_intent,
    "spacerpg.load_intent": _spacerpg_load_intent,
    "spacerpg.basic_food_sell_intent": _spacerpg_basic_food_sell_intent,
    "spacerpg.screen_trace_start": _spacerpg_screen_trace_start,
    "spacerpg.screen_trace_end": _spacerpg_screen_trace_end,
    "spacerpg.screen_trace_ordered": _spacerpg_screen_trace_ordered,
    "spacerpg.save_fingerprint_changed": _spacerpg_save_fingerprint_changed,
    "spacerpg.last_played_slot": _spacerpg_last_played_slot,
    "spacerpg.numeric_delta": _spacerpg_numeric_delta,
}


def resolve_probe(
    name: str, config: Mapping[str, Any], context: "EvaluationContext"
) -> Any:
    probe = PROBES.get(name)
    if probe is None:
        raise ValueError(f"unsupported C1 evaluation probe: {name}")
    return probe(config, context)
