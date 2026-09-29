"""Production-surface semantic grading for C1 browser GUI arms.

The A--D GUI policies receive screenshots and public task text only.  Their
semantic grader therefore must not depend on the private state hooks used by the
state-managed E arm.  This module reads only rendered DOM, production save keys,
and runtime request/error logs.  The only write is DatingSim's public Save
button round-trip: it serializes the current production state, reads it, and
restores the prior save byte-for-byte in a ``finally`` block.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from .c1_evaluation import (
    EvaluationContext,
    evaluate_checks,
    semantic_assertions,
    surface_contract,
)
from .c1_evaluation.probes import (
    spacerpg_public_assertion_collection,
    spacerpg_public_assertion_value,
    spacerpg_storage_slot_for_key,
)


LOVE_SOURCE = "love_production_save_roundtrip_v1"
SPACERPG_SOURCE = "spacerpg_production_dom_save_v1"
_LOVE_SAVE_KEY = "love_emulator_save_v1"
_SPACERPG_SAVE_INDEX_KEY = "spacerpg-save-index"
_SPACERPG_SAVE_KEYS = {
    "auto": "spacerpg-save-slot:auto",
    "manual-1": "spacerpg-save-slot:manual-1",
}
_SPACERPG_BENIGN_PAGE_ERRORS = frozenset(
    {"The root document of this element is not valid for pointer lock."}
)


def public_semantic_source(profile: Any) -> str | None:
    benchmark = str(getattr(profile, "benchmark", "")).strip().lower()
    if benchmark == "datingsim":
        return LOVE_SOURCE
    if benchmark == "spacerpg":
        return SPACERPG_SOURCE
    return None


def prepare_hook_free_entry(runtime: Any, benchmark: str) -> str:
    """Apply non-domain experimental controls and return the public entry path."""

    normalized = str(benchmark).strip().lower()
    if normalized == "datingsim":
        # Query-only mocking deliberately does not enable Love's state hook.
        runtime.block_ai_network("mocked")
        return "/?ai=mocked"
    if normalized == "spacerpg":
        # Disable the external economy service for the same deterministic market
        # used by the synthesized arms.  This key does not enable the state hook.
        runtime.install_storage_init(
            {"spacerpg-e2e-disable-economy-backend": "true"}
        )
    return "/"


def begin_public_transition_trace(runtime: Any, source: str, baseline: dict[str, Any]) -> None:
    """Start a grader-only, Python-owned trace before Phase B."""

    runtime._c1_public_transition = {  # noqa: SLF001 - runtime-scoped audit state
        "source": source,
        "baseline": baseline,
        "events": [],
        "errors": [],
    }


def record_public_transition_action(runtime: Any, action: Any, *, when: str) -> float:
    """Record grader-only evidence and return its hidden-grader latency in ms."""

    trace = getattr(runtime, "_c1_public_transition", None)
    page = getattr(runtime, "page", None)
    if not isinstance(trace, dict):
        return 0.0
    started = time.perf_counter()
    if page is None:
        trace.setdefault("errors", []).append(
            "RuntimeError: public transition page is unavailable"
        )
        return (time.perf_counter() - started) * 1000
    kind = str(getattr(action, "kind", ""))
    event: dict[str, Any] = {
        "when": when,
        "kind": kind,
        "key": getattr(action, "key", None),
        "modifiers": list(getattr(action, "modifiers", ()) or ()),
    }
    try:
        if when == "before" and kind in {"click", "double_click", "drag"}:
            x = getattr(action, "x", None)
            y = getattr(action, "y", None)
            if isinstance(x, int) and isinstance(y, int):
                event["target"] = page.evaluate(
                    r"""({x, y}) => {
                      const raw = document.elementFromPoint(x, y);
                      const target = raw?.closest?.('button,[role="button"],input,a') ?? raw;
                      const context = raw?.closest?.('.save-slot,.market-row,.action-card,.quick-actions,.station-actions');
                      const clean = (value, limit = 240) => String(value ?? '').replace(/\s+/g, ' ').trim().slice(0, limit);
                      return {
                        tag: target?.tagName?.toLowerCase() ?? null,
                        text: clean(target?.textContent),
                        title: clean(target?.getAttribute?.('title')),
                        aria_label: clean(target?.getAttribute?.('aria-label')),
                        test_id: clean(target?.getAttribute?.('data-testid')),
                        disabled: !!target?.disabled,
                        context_text: clean(context?.textContent, 400),
                        context_test_id: clean(context?.getAttribute?.('data-testid'))
                      };
                    }""",
                    {"x": x, "y": y},
                )
        event["dom"] = _public_dom_snapshot(page, trace.get("source"))
        trace.setdefault("events", []).append(event)
    except Exception as error:  # trace failure is graded fail-closed later
        trace.setdefault("errors", []).append(f"{type(error).__name__}: {error}")
    return (time.perf_counter() - started) * 1000


def grade_public_start(
    profile: Any,
    runtime: Any,
    oracle_spec: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]] | None:
    source = public_semantic_source(profile)
    if source is None:
        return None
    try:
        snapshot = _snapshot_for_source(runtime, source)
        if source == LOVE_SOURCE:
            ok, checks = _grade_love_start(snapshot, oracle_spec)
        else:
            ok, checks = _grade_spacerpg_start(snapshot, oracle_spec)
        begin_public_transition_trace(runtime, source, snapshot)
        return ok, ("success" if ok else "validate_failed"), _report_detail(
            source, snapshot, checks
        )
    except Exception as error:
        return False, "validate_error", {
            "source": source,
            "error": f"{type(error).__name__}: {error}",
        }


def grade_public_goal(
    profile: Any,
    runtime: Any,
    oracle_spec: dict[str, Any],
) -> tuple[bool, str, dict[str, Any]] | None:
    source = public_semantic_source(profile)
    if source is None:
        return None
    try:
        snapshot = _snapshot_for_source(runtime, source)
        trace = getattr(runtime, "_c1_public_transition", {})
        if not isinstance(trace, dict) or trace.get("source") != source:
            raise RuntimeError("public Phase-B trace was not initialized")
        trace_errors = trace.get("errors")
        if not isinstance(trace_errors, list):
            raise RuntimeError("public Phase-B trace errors evidence is malformed")
        if trace_errors:
            raise RuntimeError(
                "public Phase-B trace instrumentation failed: "
                + "; ".join(str(error) for error in trace_errors[:3])
            )
        baseline = trace.get("baseline")
        if not isinstance(baseline, dict):
            raise RuntimeError("public semantic baseline is missing")
        if source == LOVE_SOURCE:
            ok, checks = _grade_love_goal(snapshot, baseline, oracle_spec, runtime)
        else:
            ok, checks = _grade_spacerpg_goal(snapshot, baseline, oracle_spec, trace, runtime)
        detail = _report_detail(source, snapshot, checks)
        detail["transition_trace"] = _summarize_trace(trace)
        return ok, ("success" if ok else "assert_failed"), detail
    except Exception as error:
        return False, "assert_error", {
            "source": source,
            "error": f"{type(error).__name__}: {error}",
        }


def _snapshot_for_source(runtime: Any, source: str) -> dict[str, Any]:
    if source == LOVE_SOURCE:
        return _love_snapshot(runtime)
    if source == SPACERPG_SOURCE:
        return _spacerpg_snapshot(runtime)
    raise ValueError(f"unsupported public semantic source: {source}")


def _public_dom_snapshot(page: Any, source: str | None) -> dict[str, Any]:
    if source == LOVE_SOURCE:
        return _love_dom_snapshot(page)
    if source == SPACERPG_SOURCE:
        return _spacerpg_dom_snapshot(page)
    return {}


def _visible_js() -> str:
    return """(element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && style.display !== 'none' &&
        style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
    }"""


def _love_dom_snapshot(page: Any) -> dict[str, Any]:
    return page.evaluate(
        rf"""() => {{
          const visible = {_visible_js()};
          const statValues = {{}};
          for (const item of document.querySelectorAll('.dashboard .stat-item')) {{
            const label = String(item.querySelector('label')?.textContent ?? '').trim();
            const raw = String(item.querySelector('span')?.textContent ?? '').replace(/,/g, '').trim();
            if (label) statValues[label] = Number.isFinite(Number(raw)) ? Number(raw) : raw;
          }}
          const locations = [...document.querySelectorAll('.location-list .loc-btn')];
          const active = locations.find((button) => button.classList.contains('active'));
          const actionButtons = [...document.querySelectorAll('.action-grid .action-card')];
          const aiQuery = new URLSearchParams(location.search).get('ai');
          return {{
            screen: visible(document.querySelector('.setup-modal')) ? 'setup' :
              visible(document.querySelector('.app-container')) ? 'gameplay' : 'unknown',
            app_visible: visible(document.querySelector('.app-container')),
            dashboard_visible: visible(document.querySelector('.dashboard')),
            location_panel_visible: visible(document.querySelector('.location-panel')),
            action_grid_visible: visible(document.querySelector('.action-grid')),
            player_name: String(document.querySelector('.dashboard .status-group h2')?.textContent ?? '').trim(),
            stat_values: statValues,
            active_location_index: active ? locations.indexOf(active) : -1,
            active_location_text: String(active?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            action_buttons: actionButtons.map((button, index) => ({{
              index,
              text: String(button.textContent ?? '').replace(/\s+/g, ' ').trim().slice(0, 240),
              disabled: !!button.disabled,
              visible: visible(button)
            }})),
            ai_query_mode: aiQuery,
            ai_storage_mode: localStorage.getItem('warptest-love-ai-mode'),
            actionable_buttons: [...document.querySelectorAll('button')].filter((button) => visible(button) && !button.disabled).length
          }};
        }}"""
    )


def _love_snapshot(runtime: Any) -> dict[str, Any]:
    page = runtime.page
    dom = _love_dom_snapshot(page)
    roundtrip = page.evaluate(
        r"""() => {
          const key = 'love_emulator_save_v1';
          const prior = localStorage.getItem(key);
          let captured = null;
          let error = null;
          let restored = false;
          const buttons = [...document.querySelectorAll('.main-header .header-controls > button')];
          const saveButton = buttons[1] ?? null;
          try {
            if (!saveButton) throw new Error('public Save button is missing');
            const text = String(saveButton.textContent ?? '').trim();
            if (!/(save|保存)/i.test(text)) throw new Error(`unexpected public Save button: ${text}`);
            saveButton.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true, view: window }));
            const current = localStorage.getItem(key);
            if (current === null) throw new Error('public Save button produced no production save');
            captured = current;
          } catch (caught) {
            error = String(caught?.message ?? caught);
          } finally {
            if (prior === null) localStorage.removeItem(key);
            else localStorage.setItem(key, prior);
            restored = localStorage.getItem(key) === prior;
          }
          return {
            captured,
            prior,
            restored,
            error,
            save_button_found: !!saveButton,
            save_button_text: String(saveButton?.textContent ?? '').trim()
          };
        }"""
    )
    if roundtrip.get("restored") is not True:
        raise RuntimeError("Love production save was not restored byte-for-byte")
    prior = roundtrip.get("prior")
    surface_error = roundtrip.get("error")
    semantic_surface_error = bool(
        isinstance(surface_error, str)
        and (
            surface_error == "public Save button is missing"
            or surface_error.startswith("unexpected public Save button:")
            or surface_error == "public Save button produced no production save"
        )
    )
    if semantic_surface_error:
        # The page executed the probe and the temporary production-save write
        # was restored successfully.  A missing/unexpected public Save surface
        # therefore describes the treatment-produced application state, not an
        # evaluator outage.  Preserve it as explicit semantic evidence so the
        # grader can count a normal failure without inventing state.
        return {
            "dom": dom,
            "state": {},
            "snapshot_sha256": None,
            "prior_sha256": _sha256(prior) if isinstance(prior, str) else None,
            "prior_restored": True,
            "semantic_observation_error": str(surface_error),
            "save_button_found": roundtrip.get("save_button_found"),
            "save_button_text": roundtrip.get("save_button_text"),
            "network": _runtime_network_summary(runtime),
            "errors": _runtime_error_summary(runtime),
        }
    if surface_error:
        raise RuntimeError(str(surface_error))
    raw = roundtrip.get("captured")
    if not isinstance(raw, str):
        raise RuntimeError("Love production save capture is missing")
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"Love production save is invalid JSON: {error}") from error
    if not isinstance(state, dict):
        raise RuntimeError("Love production save is not an object")
    return {
        "dom": dom,
        "state": state,
        "snapshot_sha256": _sha256(raw),
        "prior_sha256": _sha256(prior) if isinstance(prior, str) else None,
        "prior_restored": True,
        "save_button_found": roundtrip.get("save_button_found"),
        "save_button_text": roundtrip.get("save_button_text"),
        "network": _runtime_network_summary(runtime),
        "errors": _runtime_error_summary(runtime),
    }


def _spacerpg_dom_snapshot(page: Any) -> dict[str, Any]:
    return page.evaluate(
        rf"""() => {{
          const visible = {_visible_js()};
          const station = document.querySelector('.station-screen');
          const galaxy = document.querySelector('.galaxy-panel');
          const menu = document.querySelector('.menu-screen');
          const flightRoot = document.querySelector('[data-warptest-scene="spacerpg-flight"]');
          const canvas = flightRoot?.querySelector('canvas') ?? document.querySelector('.flight-canvas canvas');
          const canvasRect = canvas?.getBoundingClientRect();
          const modalHeading = [...document.querySelectorAll('.modal-panel h2')]
            .find((heading) => visible(heading));
          const headingText = String(modalHeading?.textContent ?? '').trim();
          let screen = 'unknown';
          if (visible(station)) screen = 'station';
          else if (visible(galaxy)) screen = 'galaxyMap';
          else if (/Flight Suspended|飞行暂停|フライト停止|Vol suspendu/i.test(headingText)) screen = 'pause';
          else if (/Game Over|游戏结束|ゲームオーバー|Partie terminée/i.test(headingText)) screen = 'gameOver';
          else if (visible(menu)) screen = 'menu';
          else if (visible(flightRoot) && visible(canvas) && visible(document.querySelector('.hud'))) screen = 'flight';
          const currentSystem = document.querySelector('.galaxy-system.current');
          const dialogueOverlay = document.querySelector('.dialogue-backdrop,[data-testid="dialogue-overlay"],[data-testid="space-dialogue-overlay"]');
          const marketRow = document.querySelector('[data-testid="market-row-basic-food"]');
          const marketPanel = marketRow?.closest('.table-panel');
          const sellButton = marketRow ? [...marketRow.querySelectorAll('button')].at(-1) : null;
          const hangarRoot = document.querySelector('.hangar-build-grid');
          const hangarCargo = [...(hangarRoot?.querySelectorAll('.stat-grid span') ?? [])]
            .find((item) => /Cargo|货舱|貨物|積載|Soute/i.test(String(item.textContent ?? '')));
          return {{
            screen,
            fatal_overlay: screen === 'pause' || screen === 'gameOver' || screen === 'menu',
            blocking_overlay: visible(dialogueOverlay),
            canvas: {{
              visible: visible(canvas),
              css_width: canvasRect?.width ?? 0,
              css_height: canvasRect?.height ?? 0,
              bitmap_width: canvas?.width ?? 0,
              bitmap_height: canvas?.height ?? 0
            }},
            hud_system_text: String(document.querySelector('.hud-top-left > p')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            hud_throttle_text: String(document.querySelector('.hud-top-right')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            galaxy_mode_text: String(document.querySelector('.galaxy-header .eyebrow')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            galaxy_current_system_id: currentSystem?.getAttribute('data-system-id') ?? null,
            galaxy_known_system_ids: [...document.querySelectorAll('.galaxy-system.known')]
              .map((item) => item.getAttribute('data-system-id')).filter(Boolean),
            station_heading: String(document.querySelector('.station-header h1')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            station_active_tab: String(document.querySelector('.tab-row button.active')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            market_row_text: String(marketRow?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            market_panel_text: String(marketPanel?.querySelector(':scope > p')?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            market_sell_enabled: !!sellButton && visible(sellButton) && !sellButton.disabled,
            hangar_cargo_text: String(hangarCargo?.textContent ?? '').replace(/\s+/g, ' ').trim(),
            hangar_installed_equipment_text: [...(hangarRoot?.querySelectorAll('.hangar-scroll-panel .equipment-list-row strong') ?? [])]
              .map((item) => String(item.textContent ?? '').replace(/\s+/g, ' ').trim()),
            actionable_buttons: [...document.querySelectorAll('button')].filter((button) => visible(button) && !button.disabled).length
          }};
        }}"""
    )


def _spacerpg_snapshot(runtime: Any) -> dict[str, Any]:
    dom = _spacerpg_dom_snapshot(runtime.page)
    raw_storage = runtime.page.evaluate(
        r"""() => ({
          index: localStorage.getItem('spacerpg-save-index'),
          auto: localStorage.getItem('spacerpg-save-slot:auto'),
          manual_1: localStorage.getItem('spacerpg-save-slot:manual-1')
        })"""
    )
    storage: dict[str, Any] = {}
    for name, raw in raw_storage.items():
        if raw is None:
            storage[name] = {"exists": False, "parse_ok": True, "fingerprint": None}
            continue
        if not isinstance(raw, str):
            storage[name] = {"exists": True, "parse_ok": False, "fingerprint": None}
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            storage[name] = {
                "exists": True,
                "parse_ok": False,
                "fingerprint": _sha256(raw),
            }
            continue
        storage[name] = {
            "exists": True,
            "parse_ok": isinstance(parsed, dict),
            "fingerprint": _sha256(raw),
            "value": _summarize_spacerpg_storage(name, parsed),
        }
    return {
        "dom": dom,
        "storage": storage,
        "network": _runtime_network_summary(runtime),
        "errors": _runtime_error_summary(runtime),
    }


def _summarize_spacerpg_storage(name: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if name == "index":
        slots = value.get("slots") if isinstance(value.get("slots"), dict) else {}
        return {
            "version": value.get("version"),
            "last_played_slot": value.get("lastPlayedSlotId"),
            "slots": {
                slot: {
                    "exists": bool((slots.get(slot) or {}).get("exists"))
                    if isinstance(slots.get(slot), dict)
                    else False,
                    "screen": (slots.get(slot) or {}).get("screen")
                    if isinstance(slots.get(slot), dict)
                    else None,
                }
                for slot in ("auto", "manual-1")
            },
        }
    player = value.get("player") if isinstance(value.get("player"), dict) else {}
    cargo = player.get("cargo") if isinstance(player.get("cargo"), dict) else {}
    return {
        "version": value.get("version"),
        "saved_at": value.get("savedAt"),
        "game_clock": value.get("gameClock"),
        "screen": value.get("screen"),
        "current_system_id": value.get("currentSystemId"),
        "current_station_id": value.get("currentStationId"),
        "credits": player.get("credits"),
        "basic_food": cargo.get("basic-food"),
        "illegal_contraband": cargo.get("illegal-contraband", 0),
        "equipment": list(player.get("equipment") or []),
    }


def _declared_public_checks(
    oracle_spec: dict[str, Any],
    *,
    phase: str,
    adapter: str,
    current: dict[str, Any],
    before: dict[str, Any] | None = None,
    trace: dict[str, Any] | None = None,
    runtime: Any = None,
) -> list[dict[str, Any]] | None:
    surface = surface_contract(oracle_spec, "gui")
    if surface is None:
        return None
    if surface.get("adapter") != adapter:
        raise ValueError(
            f"C1 GUI evaluation adapter mismatch: {surface.get('adapter')!r}"
        )
    phase_contract = surface.get(phase)
    if not isinstance(phase_contract, dict):
        raise ValueError(f"C1 GUI evaluation {phase} contract is missing")
    declarations = phase_contract.get("checks")
    if not isinstance(declarations, list):
        raise ValueError(f"C1 GUI evaluation {phase}.checks is malformed")
    return evaluate_checks(
        declarations,
        EvaluationContext(
            spec=oracle_spec,
            current=current,
            before=before,
            after=current if phase == "goal" else None,
            trace=trace,
            runtime=runtime,
        ),
    )


def _grade_love_start(
    snapshot: dict[str, Any], oracle_spec: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    checks = _declared_public_checks(
        oracle_spec,
        phase="start",
        adapter=LOVE_SOURCE,
        current=snapshot,
    )
    if checks is None:
        from .c1_public_semantics_legacy import _grade_love_start_legacy

        return _grade_love_start_legacy(snapshot, oracle_spec)
    dom = snapshot["dom"]
    # This is a hook-leakage/runtime gate rather than a task metric.  Preserve
    # its historical conditional evidence shape exactly.
    if dom.get("ai_query_mode") == "mocked" and dom.get("ai_storage_mode") is None:
        _check(checks, "environment.ai_storage_mode", None, None)
    _check(checks, "probe.prior_restored", snapshot.get("prior_restored"), True)
    return _checks_ok(checks), checks


def _grade_love_goal(
    snapshot: dict[str, Any],
    baseline: dict[str, Any],
    oracle_spec: dict[str, Any],
    runtime: Any,
) -> tuple[bool, list[dict[str, Any]]]:
    declared = _declared_public_checks(
        oracle_spec,
        phase="goal",
        adapter=LOVE_SOURCE,
        current=snapshot,
        before=baseline,
        runtime=runtime,
    )
    if declared is None:
        from .c1_public_semantics_legacy import _grade_love_goal_legacy

        return _grade_love_goal_legacy(
            snapshot, baseline, oracle_spec, runtime
        )
    dom = snapshot["dom"]
    state = snapshot["state"]
    checks: list[dict[str, Any]] = []
    _check(
        checks,
        "probe.semantic_observation_error",
        snapshot.get("semantic_observation_error"),
        None,
    )
    _check(checks, "screen.gameplay", dom.get("screen"), "gameplay")
    _check(checks, "state.isSetupComplete", state.get("isSetupComplete"), True)
    for assertion in semantic_assertions(oracle_spec) or []:
        kind = assertion.get("type")
        if kind == "state_equals":
            path = str(assertion.get("path", ""))
            _check(
                checks,
                f"assert.{path}",
                _path_get(state, path),
                assertion.get("value"),
            )
        elif kind == "no_real_openai":
            attempted = _real_api_attempts(runtime)
            _check(checks, "assert.no_real_openai", attempted, [])
            _check(
                checks,
                "environment.ai_query_mode",
                dom.get("ai_query_mode"),
                "mocked",
            )
        elif kind == "no_severe_console":
            _check(
                checks,
                "assert.no_page_errors",
                snapshot["errors"].get("page"),
                [],
            )
            _check(
                checks,
                "assert.no_console_errors",
                snapshot["errors"].get("console"),
                [],
            )
        else:
            _check(checks, f"assert.unsupported.{kind}", kind, "supported")
    checks.extend(declared)
    _check(checks, "probe.prior_restored", snapshot.get("prior_restored"), True)
    return _checks_ok(checks), checks


def _grade_spacerpg_start(
    snapshot: dict[str, Any], oracle_spec: dict[str, Any]
) -> tuple[bool, list[dict[str, Any]]]:
    checks = _declared_public_checks(
        oracle_spec,
        phase="start",
        adapter=SPACERPG_SOURCE,
        current=snapshot,
    )
    if checks is None:
        from .c1_public_semantics_legacy import _grade_spacerpg_start_legacy

        return _grade_spacerpg_start_legacy(snapshot, oracle_spec)
    _check(
        checks,
        "environment.no_real_api",
        snapshot["network"].get("real_api_attempts"),
        [],
    )
    return _checks_ok(checks), checks


def _grade_spacerpg_goal(
    snapshot: dict[str, Any],
    baseline: dict[str, Any],
    oracle_spec: dict[str, Any],
    trace: dict[str, Any],
    runtime: Any,
) -> tuple[bool, list[dict[str, Any]]]:
    declared = _declared_public_checks(
        oracle_spec,
        phase="goal",
        adapter=SPACERPG_SOURCE,
        current=snapshot,
        before=baseline,
        trace=trace,
        runtime=runtime,
    )
    if declared is None:
        from .c1_public_semantics_legacy import _grade_spacerpg_goal_legacy

        return _grade_spacerpg_goal_legacy(
            snapshot, baseline, oracle_spec, trace, runtime
        )
    checks: list[dict[str, Any]] = []
    for assertion in semantic_assertions(oracle_spec) or []:
        kind = assertion.get("type")
        path = str(assertion.get("path", ""))
        if kind == "state_equals":
            actual = spacerpg_public_assertion_value(snapshot, path)
            _check(checks, f"assert.{path}", actual, assertion.get("value"))
        elif kind == "state_contains":
            expected = assertion.get("value")
            values = spacerpg_public_assertion_collection(snapshot, path)
            _check(checks, f"assert.{path}.contains", expected in values, True)
        elif kind == "state_absent":
            if path == "player.cargo.illegal-contraband":
                actual = spacerpg_public_assertion_value(snapshot, path)
                _check(checks, f"assert.{path}.absent", actual == 0, True)
            else:
                _check(checks, f"assert.unsupported.{kind}", kind, "supported")
        elif kind == "state_gte":
            actual = spacerpg_public_assertion_value(snapshot, path)
            _check_gte(checks, f"assert.{path}", actual, assertion.get("value"))
        elif kind == "localStorage_key_exists":
            slot = spacerpg_storage_slot_for_key(str(assertion.get("key", "")))
            entry = snapshot["storage"].get(slot, {}) if slot else {}
            _check(checks, f"assert.storage.{slot}.exists", entry.get("exists"), True)
            _check(
                checks,
                f"assert.storage.{slot}.parse_ok",
                entry.get("parse_ok"),
                True,
            )
        elif kind == "no_severe_console":
            _check(
                checks,
                "assert.no_page_errors",
                _spacerpg_severe_page_errors(snapshot["errors"].get("page")),
                [],
            )
            _check(
                checks,
                "assert.no_console_errors",
                snapshot["errors"].get("console"),
                [],
            )
        else:
            _check(checks, f"assert.unsupported.{kind}", kind, "supported")
    checks.extend(declared)
    _check(checks, "environment.no_real_api", _real_api_attempts(runtime), [])
    _check(checks, "trace.errors", trace.get("errors", []), [])
    return _checks_ok(checks), checks


def _runtime_network_summary(runtime: Any) -> dict[str, Any]:
    calls = getattr(runtime, "network_api_calls", {})
    return {
        "real_api_attempts": _real_api_attempts(runtime),
        "blocked_count": len(calls.get("blocked", []) or []) if isinstance(calls, dict) else 0,
        "mocked_count": len(calls.get("mocked", []) or []) if isinstance(calls, dict) else 0,
    }


def _real_api_attempts(runtime: Any) -> list[str]:
    calls = getattr(runtime, "network_api_calls", {})
    if not isinstance(calls, dict):
        return []
    urls = list(calls.get("attempted", []) or []) + list(calls.get("blocked", []) or [])
    markers = ("/api/proxy/", "/v1/chat/completions", "api.openai.com")
    return sorted({str(url) for url in urls if any(marker in str(url).lower() for marker in markers)})


def _runtime_error_summary(runtime: Any) -> dict[str, list[str]]:
    return {
        "console": [str(item)[:1000] for item in getattr(runtime, "console_errors", [])],
        "page": [str(item)[:1000] for item in getattr(runtime, "page_errors", [])],
    }


def _spacerpg_severe_page_errors(value: Any) -> Any:
    """Keep full-trial error semantics while removing one known benign browser error."""

    if not isinstance(value, list):
        return value
    return [item for item in value if item not in _SPACERPG_BENIGN_PAGE_ERRORS]


def _report_detail(
    source: str,
    snapshot: dict[str, Any],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    detail: dict[str, Any] = {
        "source": source,
        "checks": checks,
        "network": snapshot.get("network", {}),
        "errors": snapshot.get("errors", {}),
    }
    if source == LOVE_SOURCE:
        state = snapshot.get("state") if isinstance(snapshot.get("state"), dict) else {}
        detail["snapshot"] = {
            "sha256": snapshot.get("snapshot_sha256"),
            "prior_sha256": snapshot.get("prior_sha256"),
            "prior_restored": snapshot.get("prior_restored"),
            "semantic_observation_error": snapshot.get(
                "semantic_observation_error"
            ),
            "save_button_found": snapshot.get("save_button_found"),
            "save_button_text": snapshot.get("save_button_text"),
            "is_setup_complete": state.get("isSetupComplete"),
            "player_name": _path_get(state, "player.name"),
            "resources": _path_get(state, "player.resources"),
            "stats": _path_get(state, "player.stats"),
            "location": _path_get(state, "world.location"),
            "log_size": len(state.get("log", [])) if isinstance(state.get("log"), list) else None,
        }
    else:
        detail["snapshot"] = {
            "dom": snapshot.get("dom", {}),
            "storage": snapshot.get("storage", {}),
        }
    return detail


def _trace_screens(trace: dict[str, Any]) -> list[str]:
    screens: list[str] = []
    baseline = trace.get("baseline")
    if isinstance(baseline, dict):
        screen = (baseline.get("dom") or {}).get("screen")
        if isinstance(screen, str):
            screens.append(screen)
    for event in trace.get("events", []) or []:
        screen = (
            (event.get("dom") or {}).get("screen")
            if isinstance(event, dict)
            else None
        )
        if isinstance(screen, str) and (not screens or screens[-1] != screen):
            screens.append(screen)
    return screens


def _summarize_trace(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "screens": _trace_screens(trace),
        "events": [
            {
                "when": event.get("when"),
                "kind": event.get("kind"),
                "key": event.get("key"),
                "modifiers": event.get("modifiers"),
                "target": event.get("target"),
                "screen": (event.get("dom") or {}).get("screen"),
            }
            for event in trace.get("events", []) or []
            if isinstance(event, dict)
        ],
        "errors": list(trace.get("errors", []) or []),
    }


def _path_get(value: Any, path: str) -> Any:
    current = value
    for part in path.split(".") if path else []:
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else None
        else:
            return None
    return current


def _check(checks: list[dict[str, Any]], name: str, actual: Any, expected: Any) -> None:
    checks.append({"name": name, "ok": actual == expected, "actual": actual, "expected": expected})


def _check_gte(checks: list[dict[str, Any]], name: str, actual: Any, expected: Any) -> None:
    ok = isinstance(actual, (int, float)) and isinstance(expected, (int, float)) and actual >= expected
    checks.append({"name": name, "ok": ok, "actual": actual, "expected_min": expected})


def _check_greater(checks: list[dict[str, Any]], name: str, actual: Any, baseline: Any) -> None:
    ok = isinstance(actual, (int, float)) and isinstance(baseline, (int, float)) and actual > baseline
    checks.append({"name": name, "ok": ok, "actual": actual, "baseline": baseline})


def _checks_ok(checks: list[dict[str, Any]]) -> bool:
    return bool(checks) and all(check.get("ok") is True for check in checks)


def _numeric_delta(before: Any, after: Any) -> int | float | None:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return before - after
    return None


def _parse_int(value: str) -> int | None:
    cleaned = re.sub(r"[^0-9-]", "", value)
    return int(cleaned) if cleaned and cleaned not in {"-", "+"} else None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
