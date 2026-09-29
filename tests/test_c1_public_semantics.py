from __future__ import annotations

import copy
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from warptest import c1_public_semantics as semantics
from warptest.c1_public_semantics_legacy import (
    _grade_spacerpg_goal_legacy,
    _grade_spacerpg_start_legacy,
    _grade_love_goal_legacy,
    _grade_love_start_legacy,
    _love_action_id,
)
from warptest import gui_agent
from warptest import p5_aggregate
from warptest.runtime import BrowserRuntimeAdapter
from warptest.runner import load_spec


ROOT = Path(__file__).resolve().parents[1]
LOVE_SPECS = [
    "resource-action.yaml",
    "gym-workout.yaml",
    "cafe-date.yaml",
    "shopping-mall-spa.yaml",
    "office-work-coding.yaml",
]
SPACERPG_SPECS = [
    "playable-flight.yaml",
    "galaxy-jump.yaml",
    "station-save.yaml",
    "save-restore.yaml",
    "trading-cargo.yaml",
]
FORMAL_LOVE_DEEP_SPECS = [
    "c1-work-gym-date-roundtrip.yaml",
    "c1-elena-dialogue-memory-consolidation.yaml",
    "c1-elena-partner-ending-unlock.yaml",
]
FORMAL_SPACERPG_DEEP_SPECS = [
    "c1-trade-refit-mining-sortie.yaml",
    "c1-multistation-cargo-contract.yaml",
    "c1-contraband-stealth-run.yaml",
]


class _Runtime:
    def __init__(self) -> None:
        self.network_api_calls = {"attempted": [], "blocked": [], "mocked": []}
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []


class _EntryRuntime:
    def __init__(self) -> None:
        self.blocked: list[str] = []
        self.storage: dict[str, str] = {}

    def block_ai_network(self, mode: str) -> None:
        self.blocked.append(mode)

    def install_storage_init(self, values: dict[str, str]) -> None:
        self.storage.update(values)


def _spec(group: str, name: str) -> dict:
    return load_spec(ROOT / "specs" / group / name)


def _path_set(state: dict, path: str, value) -> None:
    parts = path.split(".")
    current = state
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _love_snapshot(spec: dict, *, goal: bool = False) -> dict:
    target = spec["target"]
    resources = {
        "money": 1000,
        "energy": 100,
        "health": 100,
        "mood": 100,
    }
    resources.update((target.get("patch") or {}).get("player", {}).get("resources", {}))
    location = (target.get("patch") or {}).get("world", {}).get("location", "home")
    state = {
        "isSetupComplete": True,
        "player": {
            "name": target.get("player_name", "WarpTest"),
            "gender": target.get("gender", "male"),
            "resources": resources,
            "stats": {
                "charm": 10,
                "intelligence": 10,
                "strength": 10,
                "fitness": 10,
                "communication": 10,
                "reputation": 50,
            },
        },
        "world": {"location": location},
        "log": [],
    }
    if goal:
        for assertion in spec["assertions"]:
            if assertion["type"] == "state_equals":
                _path_set(state, assertion["path"], assertion["value"])
        action_id = _love_action_id(spec)
        state["log"] = [{"text": f"Performed action: {action_id}", "type": "action"}]
    location_index = {
        "home": 0,
        "office": 1,
        "gym": 2,
        "cafe": 3,
        "restaurant": 4,
        "shopping_mall": 5,
        "bar": 6,
        "park": 7,
    }[location]
    return {
        "state": state,
        "dom": {
            "screen": "gameplay",
            "app_visible": True,
            "dashboard_visible": True,
            "location_panel_visible": True,
            "action_grid_visible": True,
            "player_name": "WarpTest",
            "active_location_index": location_index,
            "action_buttons": [
                {"disabled": False, "visible": True, "text": name}
                for name in ("Write Code", "Gym Workout", "Visit SPA", "Coffee Date")
            ],
            "ai_query_mode": "mocked",
            "ai_storage_mode": None,
            "actionable_buttons": 12,
        },
        "snapshot_sha256": "snapshot",
        "prior_sha256": None,
        "prior_restored": True,
        "network": {"real_api_attempts": []},
        "errors": {"console": [], "page": []},
    }


@pytest.mark.parametrize("name", LOVE_SPECS)
def test_love_public_grader_covers_all_c1_tasks(name: str) -> None:
    spec = _spec("love", name)
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)
    start_ok, start_checks = semantics._grade_love_start(before, spec)
    goal_ok, goal_checks = semantics._grade_love_goal(after, before, spec, _Runtime())
    assert start_ok, [check for check in start_checks if not check["ok"]]
    assert goal_ok, [check for check in goal_checks if not check["ok"]]


@pytest.mark.parametrize("name", LOVE_SPECS)
def test_love_public_goal_rejects_wrong_exact_state(name: str) -> None:
    spec = _spec("love", name)
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)
    assertion = next(item for item in spec["assertions"] if item["type"] == "state_equals")
    _path_set(after["state"], assertion["path"], assertion["value"] + 1)
    ok, _ = semantics._grade_love_goal(after, before, spec, _Runtime())
    assert not ok


def test_love_public_goal_requires_exactly_one_target_action() -> None:
    spec = _spec("love", "resource-action.yaml")
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)
    after["state"]["log"] = []
    assert not semantics._grade_love_goal(after, before, spec, _Runtime())[0]
    after["state"]["log"] = [
        {"text": "Performed action: work_coding"},
        {"text": "Performed action: work_coding"},
    ]
    assert not semantics._grade_love_goal(after, before, spec, _Runtime())[0]


@pytest.mark.parametrize("name", [*LOVE_SPECS[1:], *FORMAL_LOVE_DEEP_SPECS])
def test_declared_love_checks_match_frozen_legacy_evaluator(name: str) -> None:
    spec = _spec("love", name)
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)
    runtime = _Runtime()

    assert semantics._grade_love_start(before, spec) == _grade_love_start_legacy(
        before, spec
    )
    assert semantics._grade_love_goal(
        after, before, spec, runtime
    ) == _grade_love_goal_legacy(after, before, spec, runtime)


@pytest.mark.parametrize(
    ("name", "log"),
    [
        (
            "c1-work-gym-date-roundtrip.yaml",
            [
                {"text": "Performed action: work_coding"},
                {"text": "Performed action: gym_workout"},
                {"text": "Performed action: spa_visit"},
                {"text": "Performed action: date_coffee"},
            ],
        ),
        (
            "c1-elena-dialogue-memory-consolidation.yaml",
            [{"text": "Performed action: date_coffee"}],
        ),
        (
            "c1-elena-partner-ending-unlock.yaml",
            [
                {"text": "Performed action: date_coffee"},
                {"text": "Performed action: commit_elena_partner"},
            ],
        ),
    ],
)
def test_love_deep_contradictions_remain_current_failures(
    name: str,
    log: list[dict],
) -> None:
    spec = _spec("love", name)
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)
    after["state"]["log"] = log

    assert semantics._grade_love_goal(after, before, spec, _Runtime())[0] is False


def test_love_public_goal_rejects_real_api_and_runtime_errors() -> None:
    spec = _spec("love", "resource-action.yaml")
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)
    runtime = _Runtime()
    runtime.network_api_calls["attempted"].append("http://game/api/proxy/v1/chat/completions")
    assert not semantics._grade_love_goal(after, before, spec, runtime)[0]
    runtime.network_api_calls["attempted"].clear()
    after["errors"]["page"] = ["boom"]
    assert not semantics._grade_love_goal(after, before, spec, runtime)[0]


def _save_entry(
    fingerprint: str | None,
    *,
    slot: str,
    exists: bool = True,
) -> dict:
    if not exists:
        return {"exists": False, "parse_ok": True, "fingerprint": None}
    return {
        "exists": True,
        "parse_ok": True,
        "fingerprint": fingerprint,
        "value": {
            "version": 3,
            "saved_at": "2026-07-11T00:00:00.000Z",
            "game_clock": 1,
            "screen": "station",
            "current_system_id": "helion-reach",
            "current_station_id": "helion-prime",
            "credits": 1500,
            "basic_food": 3,
            "slot": slot,
        },
    }


def _index_entry(last_played: str = "auto", *, manual_exists: bool = False) -> dict:
    return {
        "exists": True,
        "parse_ok": True,
        "fingerprint": f"index-{last_played}",
        "value": {
            "version": 3,
            "last_played_slot": last_played,
            "slots": {
                "auto": {"exists": True, "screen": "station"},
                "manual-1": {"exists": manual_exists, "screen": "station" if manual_exists else None},
            },
        },
    }


def _spacerpg_snapshot(
    *,
    screen: str,
    system: str = "helion-reach",
    throttle: int = 0,
    cargo: int = 3,
    credits: int = 1500,
    station: str = "helion-prime",
    auto_fingerprint: str = "auto-before",
    manual_exists: bool = False,
    last_played: str = "auto",
) -> dict:
    label = "Kuro Belt" if system == "kuro-belt" else "Helion Reach"
    dom = {
        "screen": screen,
        "fatal_overlay": screen in {"pause", "gameOver", "menu"},
        "blocking_overlay": False,
        "canvas": {
            "visible": screen == "flight",
            "css_width": 1280 if screen == "flight" else 0,
            "css_height": 800 if screen == "flight" else 0,
            "bitmap_width": 1280 if screen == "flight" else 0,
            "bitmap_height": 800 if screen == "flight" else 0,
        },
        "hud_system_text": f"{label} · Terran · Risk 10%" if screen == "flight" else "",
        "hud_throttle_text": f"Throttle {throttle}% · Speed 0" if screen == "flight" else "",
        "galaxy_mode_text": "Stargate Link" if screen == "galaxyMap" else "",
        "galaxy_current_system_id": system if screen == "galaxyMap" else None,
        "galaxy_known_system_ids": ["helion-reach", "kuro-belt"] if screen == "galaxyMap" else [],
        "station_heading": (
            "Ashen Freeport" if station == "ashen-freeport" else "Helion Prime Exchange"
        ) if screen == "station" else "",
        "station_active_tab": "Market" if screen == "station" else "",
        "market_row_text": f"Basic Food · Hold {cargo} · Buy Sell" if screen == "station" else "",
        "market_panel_text": f"TL 1 · {credits:,} credits · Cargo 3/20" if screen == "station" else "",
        "market_sell_enabled": screen == "station" and cargo > 0,
        "hangar_cargo_text": f"Cargo {cargo}/28" if screen == "station" else "",
        "hangar_installed_equipment_text": [],
        "actionable_buttons": 8,
    }
    return {
        "dom": dom,
        "storage": {
            "index": _index_entry(last_played, manual_exists=manual_exists),
            "auto": _save_entry(auto_fingerprint, slot="auto"),
            "manual_1": _save_entry("manual-after", slot="manual-1", exists=manual_exists),
        },
        "network": {"real_api_attempts": []},
        "errors": {"console": [], "page": []},
    }


def _event(
    screen: str,
    *,
    when: str = "after",
    kind: str = "click",
    key: str | None = None,
    text: str = "",
    context: str = "",
    test_id: str = "",
) -> dict:
    return {
        "when": when,
        "kind": kind,
        "key": key,
        "target": {
            "text": text,
            "title": "",
            "aria_label": "",
            "context_text": context,
            "context_test_id": test_id,
        },
        "dom": {"screen": screen},
    }


def _spacerpg_case(name: str) -> tuple[dict, dict, dict]:
    spec = _spec("spacerpg", name)
    if name == "playable-flight.yaml":
        before = _spacerpg_snapshot(screen="flight", throttle=0)
        after = _spacerpg_snapshot(screen="flight", throttle=12)
        events = [
            _event("flight", when="before", kind="key", key="W"),
            _event("flight"),
        ]
    elif name == "galaxy-jump.yaml":
        before = _spacerpg_snapshot(screen="galaxyMap")
        after = _spacerpg_snapshot(screen="flight", system="kuro-belt", throttle=0)
        events = [_event("galaxyMap", when="before", text="Kuro Belt"), _event("flight")]
    elif name == "station-save.yaml":
        before = _spacerpg_snapshot(screen="station", auto_fingerprint="auto-before")
        after = _spacerpg_snapshot(screen="station", auto_fingerprint="auto-after")
        events = [
            _event("station", when="before", text="Quick Save", context="Quick Save Settings Launch"),
            _event("station"),
        ]
    elif name == "save-restore.yaml":
        before = _spacerpg_snapshot(screen="station", manual_exists=False)
        after = _spacerpg_snapshot(screen="station", manual_exists=True, last_played="manual-1")
        events = [
            _event("station", when="before", text="Save", context="Manual Slot 1 Save Load Delete"),
            _event("flight"),
            _event("flight", when="before", text="Load", context="Manual Slot 1 Save Load Delete"),
            _event("station"),
        ]
    elif name == "c1-trade-refit-mining-sortie.yaml":
        # This task starts in the Helion Prime hangar with 15 cargo units and a
        # pulse laser installed, so the public surface exposes the hangar panel
        # rather than a market row.
        before = _spacerpg_snapshot(screen="station", cargo=15, credits=10000)
        before["dom"]["station_active_tab"] = "Hangar"
        before["dom"]["hangar_cargo_text"] = "Cargo 15/18"
        before["dom"]["hangar_installed_equipment_text"] = ["Pulse Laser"]
        before["dom"]["market_row_text"] = ""
        before["dom"]["market_sell_enabled"] = False
        after = _spacerpg_snapshot(screen="station", cargo=14, credits=10726)
        events = [
            _event(
                "station",
                when="before",
                text="Sell",
                context="Basic Food Hold 15 Buy Sell",
                test_id="market-row-basic-food",
            ),
            _event("station"),
        ]
    elif name == "c1-contraband-stealth-run.yaml":
        before = _spacerpg_snapshot(screen="station", cargo=0, credits=1725)
        before["dom"]["station_active_tab"] = "Hangar"
        before["dom"]["market_sell_enabled"] = False
        before["dom"]["market_panel_text"] = ""
        before["storage"]["auto"]["value"].update(
            credits=1725,
            illegal_contraband=0,
            equipment=["pulse-laser"],
        )
        after = _spacerpg_snapshot(
            screen="station",
            system="ashen-drift",
            station="ashen-freeport",
            cargo=0,
            credits=1800,
        )
        after["dom"]["station_active_tab"] = "Hangar"
        after["dom"]["market_panel_text"] = ""
        after["dom"]["hangar_installed_equipment_text"] = [
            "Pulse Laser",
            "Shielded Holds",
            "Decoy Transponder",
        ]
        after["storage"]["auto"]["value"].update(
            current_system_id="ashen-drift",
            current_station_id="ashen-freeport",
            credits=1800,
            illegal_contraband=0,
            equipment=["pulse-laser", "shielded-holds", "decoy-transponder"],
        )
        events = [_event("station")]
    else:
        before = _spacerpg_snapshot(screen="station", cargo=3, credits=1500)
        after = _spacerpg_snapshot(screen="station", cargo=2, credits=1726)
        events = [
            _event(
                "station",
                when="before",
                text="Sell",
                context="Basic Food Hold 3 Buy Sell",
                test_id="market-row-basic-food",
            ),
            _event("station"),
        ]
    return spec, before, {"snapshot": after, "trace": {"source": semantics.SPACERPG_SOURCE, "baseline": before, "events": events, "errors": []}}


@pytest.mark.parametrize("name", SPACERPG_SPECS)
def test_spacerpg_public_grader_covers_all_c1_tasks(name: str) -> None:
    spec, before, bundle = _spacerpg_case(name)
    start_ok, start_checks = semantics._grade_spacerpg_start(before, spec)
    goal_ok, goal_checks = semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )
    assert start_ok, [check for check in start_checks if not check["ok"]]
    assert goal_ok, [check for check in goal_checks if not check["ok"]]


_CORRECTED_SPACERPG_SPECS = {
    "c1-contraband-stealth-run.yaml",
    # Its GUI start contract asserted the market tab while its own oracle target
    # declares a hangar start, so the production surface rejected a correct
    # restore that the hook validator accepted.
    "c1-trade-refit-mining-sortie.yaml",
}


@pytest.mark.parametrize(
    "name",
    [
        *SPACERPG_SPECS,
        *(name for name in FORMAL_SPACERPG_DEEP_SPECS if name not in _CORRECTED_SPACERPG_SPECS),
    ],
)
def test_declared_spacerpg_checks_match_frozen_legacy_evaluator(name: str) -> None:
    spec, before, bundle = _spacerpg_case(name)
    runtime = _Runtime()

    assert semantics._grade_spacerpg_start(before, spec) == _grade_spacerpg_start_legacy(
        before, spec
    )
    assert semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], runtime
    ) == _grade_spacerpg_goal_legacy(
        bundle["snapshot"], before, spec, bundle["trace"], runtime
    )


@pytest.mark.parametrize(
    "name",
    [name for name in FORMAL_SPACERPG_DEEP_SPECS if name not in _CORRECTED_SPACERPG_SPECS],
)
def test_spacerpg_deep_contradictions_remain_current_failures(name: str) -> None:
    spec, before, bundle = _spacerpg_case(name)

    assert semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )[0] is False


def test_corrected_spacerpg_contraband_public_checks_match_its_own_task() -> None:
    spec, before, bundle = _spacerpg_case("c1-contraband-stealth-run.yaml")

    start_ok, start_checks = semantics._grade_spacerpg_start(before, spec)
    goal_ok, goal_checks = semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )

    assert start_ok, [check for check in start_checks if not check["ok"]]
    assert goal_ok, [check for check in goal_checks if not check["ok"]]


def test_corrected_spacerpg_mining_sortie_start_reads_the_hangar_it_declares() -> None:
    """The production surface must accept the hangar start the oracle declares.

    The contract previously required the market tab, the per-item market row, and
    an enabled sell button -- none of which a hangar start renders -- so the GUI
    arms scored a fair zero on a Start the state arm passed.
    """

    spec, before, _bundle = _spacerpg_case("c1-trade-refit-mining-sortie.yaml")

    start_ok, start_checks = semantics._grade_spacerpg_start(before, spec)

    assert start_ok, [check for check in start_checks if not check["ok"]]
    graded = {check["name"] for check in start_checks}
    assert "dom.hangar_installed_equipment" in graded
    assert "dom.market_sell_enabled" not in graded


@pytest.mark.parametrize(
    ("station_heading", "station_tab", "cargo_text", "equipment_text"),
    [
        (
            "灰烬自由港",
            "机库",
            "货舱 0/28",
            ["脉冲激光", "屏蔽货舱", "诱饵应答器"],
        ),
        (
            "灰燼自由港",
            "ハンガー",
            "積載 0/28",
            ["パルスレーザー", "遮蔽貨物庫", "デコイトランスポンダー"],
        ),
        (
            "Port libre cendré",
            "Hangar",
            "Soute 0/28",
            ["Laser à impulsion", "Soutes blindées", "Transpondeur leurre"],
        ),
    ],
)
def test_spacerpg_contraband_goal_accepts_localized_hangar(
    station_heading: str,
    station_tab: str,
    cargo_text: str,
    equipment_text: list[str],
) -> None:
    spec, before, bundle = _spacerpg_case("c1-contraband-stealth-run.yaml")
    dom = bundle["snapshot"]["dom"]
    dom.update(
        station_heading=station_heading,
        station_active_tab=station_tab,
        hangar_cargo_text=cargo_text,
        hangar_installed_equipment_text=equipment_text,
    )

    goal_ok, goal_checks = semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )

    assert goal_ok, [check for check in goal_checks if not check["ok"]]


def test_spacerpg_public_credits_preserve_visible_zero_over_saved_value() -> None:
    snapshot = _spacerpg_snapshot(screen="station", credits=0)
    snapshot["storage"]["auto"]["value"]["credits"] = 1725

    assert semantics.spacerpg_public_assertion_value(
        snapshot, "player.credits"
    ) == 0


def test_spacerpg_gate_mode_rejects_route_planning() -> None:
    spec, before, _ = _spacerpg_case("galaxy-jump.yaml")
    before["dom"]["galaxy_mode_text"] = "Route Planning"
    assert not semantics._grade_spacerpg_start(before, spec)[0]


def test_spacerpg_auto_save_requires_phase_b_change_and_intent() -> None:
    spec, before, bundle = _spacerpg_case("station-save.yaml")
    bundle["snapshot"]["storage"]["auto"]["fingerprint"] = before["storage"]["auto"]["fingerprint"]
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]
    bundle["snapshot"]["storage"]["auto"]["fingerprint"] = "changed"
    bundle["trace"]["events"] = [_event("station")]
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]


def test_spacerpg_auto_save_ignores_known_benign_pointer_lock_pageerror() -> None:
    spec, before, bundle = _spacerpg_case("station-save.yaml")
    benign = "The root document of this element is not valid for pointer lock."
    before["errors"]["page"] = [benign]
    bundle["snapshot"]["errors"]["page"] = [benign]

    ok, checks = semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )

    assert ok, [check for check in checks if not check["ok"]]
    page_check = next(
        check for check in checks if check["name"] == "assert.no_page_errors"
    )
    assert page_check["actual"] == []


@pytest.mark.parametrize(
    ("channel", "error", "preexisting"),
    [
        ("page", "Phase A page failure", True),
        ("console", "Phase A console failure", True),
        ("page", "Phase B page failure", False),
        ("console", "Phase B console failure", False),
        (
            "page",
            "The root document of this element is not valid for pointer lock. extra",
            False,
        ),
    ],
)
def test_spacerpg_auto_save_rejects_real_runtime_errors_alongside_benign_pointer_lock(
    channel: str,
    error: str,
    preexisting: bool,
) -> None:
    spec, before, bundle = _spacerpg_case("station-save.yaml")
    benign = "The root document of this element is not valid for pointer lock."
    before["errors"]["page"] = [benign]
    bundle["snapshot"]["errors"]["page"] = [benign]
    if preexisting:
        before["errors"][channel].append(error)
    bundle["snapshot"]["errors"][channel].append(error)

    ok, checks = semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )

    assert not ok
    check_name = (
        "assert.no_page_errors" if channel == "page" else "assert.no_console_errors"
    )
    error_check = next(check for check in checks if check["name"] == check_name)
    assert error_check["actual"] == [error]


def test_spacerpg_intent_verbs_do_not_leak_from_container_context() -> None:
    spec, before, bundle = _spacerpg_case("station-save.yaml")
    bundle["trace"]["events"][0] = _event(
        "station",
        when="before",
        text="Launch",
        context="Quick Save Settings Launch",
    )
    assert not semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )[0]

    spec, before, bundle = _spacerpg_case("save-restore.yaml")
    bundle["trace"]["events"] = [
        _event(
            "station",
            when="before",
            text="Save",
            context="Manual Slot 1 Save Load Delete",
        ),
        _event("flight"),
        _event("station"),
    ]
    assert not semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )[0]

    spec, before, bundle = _spacerpg_case("trading-cargo.yaml")
    bundle["trace"]["events"][0] = _event(
        "station",
        when="before",
        text="Buy",
        context="Basic Food Hold 3 Buy Sell",
        test_id="market-row-basic-food",
    )
    assert not semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )[0]


def test_spacerpg_manual_restore_requires_flight_trace_and_last_played() -> None:
    spec, before, bundle = _spacerpg_case("save-restore.yaml")
    no_flight = copy.deepcopy(bundle["trace"])
    for event in no_flight["events"]:
        event["dom"]["screen"] = "station"
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, no_flight, _Runtime())[0]
    bundle["snapshot"]["storage"]["index"]["value"]["last_played_slot"] = "auto"
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]


def test_spacerpg_manual_restore_rejects_preexisting_manual_start() -> None:
    spec, before, _ = _spacerpg_case("save-restore.yaml")
    before["storage"]["manual_1"] = _save_entry("preexisting", slot="manual-1")
    assert not semantics._grade_spacerpg_start(before, spec)[0]


def test_spacerpg_trade_rejects_double_sale_and_low_credits_without_requiring_save() -> None:
    spec, before, bundle = _spacerpg_case("trading-cargo.yaml")
    bundle["snapshot"]["dom"]["market_row_text"] = "Basic Food · Hold 1 · Buy Sell"
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]
    bundle["snapshot"]["dom"]["market_row_text"] = "Basic Food · Hold 2 · Buy Sell"
    bundle["snapshot"]["dom"]["market_panel_text"] = "TL 1 · 1725 credits · Cargo 2/20"
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]


def test_spacerpg_flight_requires_real_throttle_change_and_one_w_intent() -> None:
    spec, before, bundle = _spacerpg_case("playable-flight.yaml")
    bundle["snapshot"]["dom"]["hud_throttle_text"] = "Throttle 0% · Speed 0"
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]
    bundle["snapshot"]["dom"]["hud_throttle_text"] = "Throttle 12% · Speed 1"
    bundle["trace"]["events"].insert(0, _event("flight", when="before", kind="key", key="W"))
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]


@pytest.mark.parametrize(("intent_count", "expected"), [(0, False), (1, True), (2, False)])
def test_spacerpg_flight_throttle_requires_exactly_one_intent(
    intent_count: int,
    expected: bool,
) -> None:
    spec, before, bundle = _spacerpg_case("playable-flight.yaml")
    bundle["trace"]["events"] = [
        *[
            _event("flight", when="before", kind="key", key="W")
            for _ in range(intent_count)
        ],
        _event("flight"),
    ]

    assert semantics._grade_spacerpg_goal(
        bundle["snapshot"], before, spec, bundle["trace"], _Runtime()
    )[0] is expected


def test_spacerpg_overlay_never_counts_as_flight() -> None:
    spec, before, bundle = _spacerpg_case("playable-flight.yaml")
    bundle["snapshot"]["dom"].update(screen="pause", fatal_overlay=True)
    assert not semantics._grade_spacerpg_goal(bundle["snapshot"], before, spec, bundle["trace"], _Runtime())[0]


def test_spacerpg_dialogue_overlay_blocks_usable_station_start() -> None:
    spec, before, _ = _spacerpg_case("station-save.yaml")
    before["dom"]["blocking_overlay"] = True
    assert not semantics._grade_spacerpg_start(before, spec)[0]


def test_public_goal_promotes_transition_probe_errors_to_assert_error(
    monkeypatch,
) -> None:
    spec, _before, bundle = _spacerpg_case("station-save.yaml")
    bundle["trace"]["errors"] = ["RuntimeError: evaluator unavailable"]
    runtime = _Runtime()
    runtime._c1_public_transition = bundle["trace"]
    monkeypatch.setattr(
        semantics,
        "_snapshot_for_source",
        lambda *_args: bundle["snapshot"],
    )

    ok, status, detail = semantics.grade_public_goal(
        SimpleNamespace(benchmark="SpaceRPG"), runtime, spec
    )
    assert not ok
    assert status == "assert_error"
    assert "trace instrumentation failed" in detail["error"]


def test_spacerpg_public_probe_is_read_only_and_hook_free() -> None:
    source = inspect.getsource(semantics._spacerpg_snapshot) + inspect.getsource(semantics._spacerpg_dom_snapshot)
    for forbidden in (
        "setItem",
        "removeItem",
        ".clear(",
        ".reload(",
        ".click(",
        ".keyboard",
        "getState",
        "setState",
        "readSave",
        "readSaveIndex",
    ):
        assert forbidden not in source


def test_love_public_probe_uses_only_public_save_roundtrip() -> None:
    source = inspect.getsource(semantics._love_snapshot)
    assert "dispatchEvent" in source
    assert "love_emulator_save_v1" in source
    for forbidden in ("getState", "patchState", "setState"):
        assert forbidden not in source


def _love_probe_runtime(roundtrip: dict) -> SimpleNamespace:
    page = MagicMock()
    page.evaluate.side_effect = [
        {
            "screen": "unknown",
            "app_visible": False,
            "dashboard_visible": False,
            "location_panel_visible": False,
            "action_grid_visible": False,
            "player_name": "",
            "stat_values": {},
            "active_location_index": -1,
            "active_location_text": "",
            "action_buttons": [],
            "ai_query_mode": "mocked",
            "ai_storage_mode": None,
            "actionable_buttons": 0,
        },
        roundtrip,
    ]
    return SimpleNamespace(
        page=page,
        network_api_calls={"attempted": [], "blocked": [], "mocked": []},
        console_errors=[],
        page_errors=[],
    )


def test_love_missing_public_save_is_counted_semantic_failure() -> None:
    runtime = _love_probe_runtime(
        {
            "captured": None,
            "prior": None,
            "restored": True,
            "error": "public Save button is missing",
            "save_button_found": False,
            "save_button_text": "",
        }
    )

    ok, status, detail = semantics.grade_public_start(
        SimpleNamespace(benchmark="DatingSim"),
        runtime,
        _spec("love", "gym-workout.yaml"),
    )

    assert not ok
    assert status == "validate_failed"
    assert detail["snapshot"]["semantic_observation_error"] == (
        "public Save button is missing"
    )
    assert detail["snapshot"]["save_button_found"] is False
    assert any(
        check["name"] == "probe.semantic_observation_error"
        and check["ok"] is False
        for check in detail["checks"]
    )


def test_love_probe_restore_failure_remains_incomplete_error() -> None:
    runtime = _love_probe_runtime(
        {
            "captured": None,
            "prior": "old-save",
            "restored": False,
            "error": "public Save button is missing",
            "save_button_found": False,
            "save_button_text": "",
        }
    )

    ok, status, detail = semantics.grade_public_start(
        SimpleNamespace(benchmark="DatingSim"),
        runtime,
        _spec("love", "gym-workout.yaml"),
    )

    assert not ok
    assert status == "validate_error"
    assert "not restored byte-for-byte" in detail["error"]


def test_love_unknown_probe_exception_remains_incomplete_error() -> None:
    runtime = _love_probe_runtime(
        {
            "captured": None,
            "prior": None,
            "restored": True,
            "error": "localStorage access denied",
            "save_button_found": True,
            "save_button_text": "Save",
        }
    )

    ok, status, detail = semantics.grade_public_start(
        SimpleNamespace(benchmark="DatingSim"),
        runtime,
        _spec("love", "gym-workout.yaml"),
    )

    assert not ok
    assert status == "validate_error"
    assert "localStorage access denied" in detail["error"]


def test_public_entry_controls_keep_from_start_domain_state_uninjected() -> None:
    love = _EntryRuntime()
    assert semantics.prepare_hook_free_entry(love, "DatingSim") == "/?ai=mocked"
    assert love.blocked == ["mocked"]
    assert love.storage == {}

    spacerpg = _EntryRuntime()
    assert semantics.prepare_hook_free_entry(spacerpg, "SpaceRPG") == "/"
    assert spacerpg.storage == {"spacerpg-e2e-disable-economy-backend": "true"}


def test_public_semantic_source_routes_only_love_and_spacerpg() -> None:
    assert semantics.public_semantic_source(SimpleNamespace(benchmark="DatingSim")) == semantics.LOVE_SOURCE
    assert semantics.public_semantic_source(SimpleNamespace(benchmark="SpaceRPG")) == semantics.SPACERPG_SOURCE
    assert semantics.public_semantic_source(SimpleNamespace(benchmark="2048")) is None


def test_runtime_request_audit_records_real_ai_endpoint_attempts_only() -> None:
    runtime = BrowserRuntimeAdapter.__new__(BrowserRuntimeAdapter)
    runtime.network_api_calls = {"attempted": [], "blocked": [], "mocked": []}
    runtime._on_request(SimpleNamespace(url="http://game/assets/ship.png"))
    runtime._on_request(SimpleNamespace(url="http://game/api/proxy/v1/chat/completions"))
    assert runtime.network_api_calls["attempted"] == [
        "http://game/api/proxy/v1/chat/completions"
    ]


def test_public_probe_trace_never_enters_policy_kwargs(monkeypatch, tmp_path: Path) -> None:
    marker = "SECRET_SEMANTIC_PROBE_PAYLOAD"

    class _Policy:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def next_action(self, **kwargs):
            self.calls.append(kwargs)
            return {"action": "click", "x": 10, "y": 20} if len(self.calls) == 1 else {"action": "done"}

    policy = _Policy()
    runtime = SimpleNamespace(
        _c1_public_transition={
            "source": semantics.SPACERPG_SOURCE,
            "baseline": {"dom": {"screen": "station"}, "secret": marker},
            "events": [],
            "errors": [],
        }
    )
    monkeypatch.setattr(gui_agent, "_capture_runtime", lambda *_args, **_kwargs: str(tmp_path / "frame.png"))
    monkeypatch.setattr(gui_agent, "_apply_action", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        gui_agent,
        "record_public_transition_action",
        lambda *_args, **_kwargs: runtime._c1_public_transition["events"].append({"secret": marker}),
    )
    gui_agent._run_phase(
        runtime=runtime,
        client=policy,
        instruction="public task instruction",
        phase_dir=tmp_path,
        max_steps=2,
        task_id="probe-no-leak",
    )
    assert marker not in repr(policy.calls)


def test_phase_excludes_hidden_probe_time_and_uses_effective_coordinates(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _Policy:
        calls = 0

        def next_action(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return gui_agent.AgentAction(kind="click", x=-20, y=900)
            return gui_agent.AgentAction(kind="done")

    applied: list[gui_agent.AgentAction] = []
    traced: list[gui_agent.AgentAction] = []
    monkeypatch.setattr(
        gui_agent,
        "_capture_runtime",
        lambda *_args, **_kwargs: str(tmp_path / "frame.png"),
    )
    monkeypatch.setattr(
        gui_agent,
        "_apply_action",
        lambda _runtime, action: applied.append(action),
    )

    def record(_runtime, action, *, when):
        assert when in {"before", "after"}
        traced.append(action)
        return 7.5

    monkeypatch.setattr(gui_agent, "record_public_transition_action", record)
    phase = gui_agent._run_phase(
        runtime=SimpleNamespace(),
        client=_Policy(),
        instruction="click",
        phase_dir=tmp_path,
        max_steps=2,
    )

    assert 15.0 <= phase.grader_ms < 16.0
    assert phase.elapsed_ms == 0
    assert [(action.x, action.y) for action in applied] == [(0, 799)]
    assert [(action.x, action.y) for action in traced] == [(0, 799), (0, 799)]


def test_phase_captures_fresh_terminal_frame_after_policy_decision(
    monkeypatch,
    tmp_path: Path,
) -> None:
    class _Policy:
        def next_action(self, **_kwargs):
            return gui_agent.AgentAction(kind="done")

    ticks = iter((0.0, 0.010, 0.020, 0.020))
    monkeypatch.setattr(gui_agent.time, "perf_counter", lambda: next(ticks))
    step_frame = str(tmp_path / "step-frame.png")
    final_frame = str(tmp_path / "final-frame.png")
    capture = MagicMock(side_effect=(step_frame, final_frame))
    monkeypatch.setattr(gui_agent, "_capture_runtime", capture)
    phase = gui_agent._run_phase(
        runtime=SimpleNamespace(),
        client=_Policy(),
        instruction="done",
        phase_dir=tmp_path,
        max_steps=1,
    )

    assert phase.elapsed_ms == 10
    assert phase.grader_ms == 10.0
    assert phase.screenshot == final_frame
    assert capture.call_count == 2


def test_formal_gate_rejects_missing_public_semantic_attestation() -> None:
    source = semantics.LOVE_SOURCE
    before_detail = {
        "source": source,
        "checks": [{"name": "fixture.semantic", "ok": True}],
        "network": {},
        "errors": {},
        "snapshot": {"prior_restored": True},
    }
    after_detail = copy.deepcopy(before_detail)
    after_detail["transition_trace"] = {
        "screens": [],
        "events": [],
        "errors": [],
    }
    trial = {
        "arm": "c1_gui",
        "model": "glm-5.2",
        "task_id": "love-resource-action",
        "seed": 0,
        "benchmark": "DatingSim",
        "semantic_probe_source": source,
        "policy_action_interface": "pixel_mouse_keyboard_only",
        "policy_received_semantic_probe": False,
        "policy_runtime_attestation": {
            "required": True,
            "passed": True,
            "entry_surface": "public_ai_query_mock",
            "private_hook_present": False,
            "ai_query_mode": "mocked",
            "ai_storage_mode": None,
        },
        "before_semantic_ok": True,
        "after_semantic_ok": True,
        "before_semantic_detail": before_detail,
        "after_semantic_detail": after_detail,
    }
    assert p5_aggregate._c1_public_semantic_trial_issues(trial) == []
    trial["policy_received_semantic_probe"] = None
    assert any(
        "leakage attestation" in issue
        for issue in p5_aggregate._c1_public_semantic_trial_issues(trial)
    )


def test_formal_gate_rejects_semantic_check_and_attestation_tampering() -> None:
    source = semantics.LOVE_SOURCE
    before_detail = {
        "source": source,
        "checks": [{"name": "fixture.semantic", "ok": False}],
        "network": {},
        "errors": {},
        "snapshot": {"prior_restored": True},
    }
    after_detail = copy.deepcopy(before_detail)
    after_detail["transition_trace"] = {
        "screens": [],
        "events": [],
        "errors": [],
    }
    trial = {
        "arm": "c1_gui",
        "model": "glm-5.2",
        "task_id": "love-resource-action",
        "seed": 0,
        "benchmark": "DatingSim",
        "semantic_probe_source": source,
        "policy_action_interface": "pixel_mouse_keyboard_only",
        "policy_received_semantic_probe": False,
        "policy_runtime_attestation": {
            "required": True,
            "passed": True,
            "entry_surface": "public_ai_query_mock",
            "private_hook_present": True,
            "ai_query_mode": "mocked",
            "ai_storage_mode": None,
        },
        "before_semantic_ok": True,
        "after_semantic_ok": True,
        "before_semantic_detail": before_detail,
        "after_semantic_detail": after_detail,
    }
    issues = p5_aggregate._c1_public_semantic_trial_issues(trial)
    assert any("does not match all" in issue for issue in issues)
    assert any("private_hook_present must be False" in issue for issue in issues)
    trial["policy_runtime_attestation"].pop("ai_query_mode")
    assert any(
        "ai_query_mode must be 'mocked'" in issue
        for issue in p5_aggregate._c1_public_semantic_trial_issues(trial)
    )
    trial["after_semantic_detail"]["transition_trace"]["errors"] = [
        "evaluator unavailable"
    ]
    assert any(
        "transition_trace contains instrumentation errors" in issue
        for issue in p5_aggregate._c1_public_semantic_trial_issues(trial)
    )


def test_gui_semantic_entrypoints_do_not_fall_back_to_private_love_profile(
    monkeypatch,
) -> None:
    spec = _spec("love", "resource-action.yaml")
    before = _love_snapshot(spec)
    after = _love_snapshot(spec, goal=True)

    class _PrivateProfile:
        benchmark = "DatingSim"

        def _validate(self, *_args):
            raise AssertionError("private validator must not run")

        def _assert_oracles(self, *_args):
            raise AssertionError("private oracle must not run")

    runtime = _Runtime()
    snapshots = iter([before, after])
    monkeypatch.setattr(semantics, "_snapshot_for_source", lambda *_args: next(snapshots))
    start = gui_agent._semantic_start_grade(_PrivateProfile(), runtime, spec)
    goal = gui_agent._semantic_grade_live(
        _PrivateProfile(), runtime, spec, before_score=0
    )
    assert start[:2] == (True, "success")
    assert goal[:2] == (True, "success")
