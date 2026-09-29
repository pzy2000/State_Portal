from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .c1_evaluation import EvaluationContext, evaluate_checks, surface_contract
from .discovery import RepositoryInspector
from .reports import write_json
from .runtime import BrowserRuntimeAdapter
from .task_schema import PublicIssue, resolve_task_repo


FORBIDDEN_GUI_BASELINE_ACTIONS = {"setState", "jumpToSystem", "dockAt", "applyDebugScenario"}


def run_spacerpg_gui_from_start_baseline(
    task: PublicIssue,
    hidden_spec: dict[str, Any] | None,
    *,
    root: str | Path,
    report_dir: Path | None,
    headless: bool = True,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    out_dir = report_dir or (base / "reports" / "warptest" / "spacerpg-gui-baseline" / task.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_surface = surface_contract(
        hidden_spec if isinstance(hidden_spec, dict) else {},
        "gui_from_start_baseline",
    )
    if baseline_surface is None:
        result = _static_not_reached(task, "This task requires an injection-only deep state that is not reachable from strict GUI-from-start within the budget.")
        write_json(out_dir / "gui-from-start.json", result)
        return result
    recipe = _baseline_recipe(baseline_surface)

    repo = resolve_task_repo(base, task)
    launch = RepositoryInspector().inspect(repo).metadata.launch_command
    trace: list[dict[str, Any]] = []
    start = time.perf_counter()
    result: dict[str, Any] = {
        "status": "not_reached",
        "public_inputs_only": True,
        "strict_no_checkpoint": True,
        "actions_public_only": True,
        "hidden_oracle_used_only_by_grader": True,
        "budget_ms": task.budget_ms,
        "task_id": task.id,
        "benchmark": task.benchmark,
        "action_trace": trace,
        "forbidden_action_calls": [],
        "screenshots": [],
        "console_errors": [],
        "severe_console_errors": [],
    }
    try:
        with BrowserRuntimeAdapter(repo, launch, out_dir, headless=headless) as runtime:
            runtime.context.add_init_script(
                """() => {
                  window.localStorage.setItem("spacerpg-e2e-disable-economy-backend", "true");
                }"""
            )
            runtime.open("/")
            _start_new_game(runtime, trace)
            _run_task_path(runtime, recipe, trace)
            result["screenshots"].append(runtime.screenshot("gui-from-start-final.png"))
            grader = _grade_spacerpg_gui_baseline(runtime, task, hidden_spec)
            result["hidden_grader"] = grader
            result["status"] = grader["status"]
            result["reason"] = grader.get("reason", "")
            result["console_errors"] = runtime.console_errors + runtime.page_errors
            result["severe_console_errors"] = runtime.console_errors + runtime.page_errors
    except Exception as error:
        result["status"] = "timeout" if _elapsed_ms(start) >= task.budget_ms else "not_reached"
        result["reason"] = str(error)
    result["time_to_target_ms"] = _elapsed_ms(start)
    result["forbidden_action_calls"] = forbidden_gui_action_calls(trace)
    if result["forbidden_action_calls"]:
        result["status"] = "failure"
        result["reason"] = "GUI baseline action trace used forbidden checkpoint/state hook calls."
    write_json(out_dir / "gui-from-start.json", result)
    return result


def forbidden_gui_action_calls(action_trace: list[dict[str, Any]]) -> list[str]:
    calls: list[str] = []
    for action in action_trace:
        text = json.dumps(action, sort_keys=True)
        for forbidden in FORBIDDEN_GUI_BASELINE_ACTIONS:
            if forbidden in text:
                calls.append(forbidden)
    return sorted(set(calls))


def _static_not_reached(task: PublicIssue, reason: str) -> dict[str, Any]:
    return {
        "status": "not_reached",
        "public_inputs_only": True,
        "strict_no_checkpoint": True,
        "actions_public_only": True,
        "hidden_oracle_used_only_by_grader": True,
        "budget_ms": task.budget_ms,
        "task_id": task.id,
        "benchmark": task.benchmark,
        "action_trace": [],
        "forbidden_action_calls": [],
        "screenshots": [],
        "console_errors": [],
        "severe_console_errors": [],
        "reason": reason,
        "time_to_target_ms": None,
    }


def _start_new_game(runtime: BrowserRuntimeAdapter, trace: list[dict[str, Any]]) -> None:
    page = runtime.page
    _click_button(page, trace, re.compile(r"New (Offline )?Game"))
    page.locator(".flight-canvas canvas").wait_for(state="visible", timeout=20_000)
    _maybe_skip_dialogue(page, trace)
    page.locator(".dock-hint").wait_for(state="visible", timeout=10_000)


def _run_task_path(runtime: BrowserRuntimeAdapter, recipe: str, trace: list[dict[str, Any]]) -> None:
    if recipe == "playable_flight":
        _press(runtime.page, trace, "W")
        runtime.page.wait_for_timeout(300)
        return
    if recipe in {"station_save", "save_restore", "trading_cargo"}:
        _dock_at_start_station(runtime.page, trace)
        if recipe == "station_save":
            _click_button(runtime.page, trace, "Quick Save")
            runtime.page.wait_for_timeout(300)
            return
        if recipe == "save_restore":
            _click_button(runtime.page, trace, "Hangar")
            _click_save_slot(runtime.page, trace, "Manual 1")
            _click_button(runtime.page, trace, "Launch")
            runtime.page.locator(".flight-canvas canvas").wait_for(state="visible", timeout=10_000)
            runtime.page.reload(wait_until="domcontentloaded")
            _click_button(runtime.page, trace, re.compile(r"Continue( Offline)?"))
            runtime.page.wait_for_timeout(500)
            return
        if recipe == "trading_cargo":
            row = runtime.page.get_by_test_id("market-row-basic-food")
            row.get_by_role("button", name="Sell").click(timeout=10_000)
            trace.append({"type": "click", "target": "market-row-basic-food Sell"})
            _click_button(runtime.page, trace, "Quick Save")
            runtime.page.wait_for_timeout(300)
            return
    if recipe == "galaxy_jump":
        _press(runtime.page, trace, "KeyM")
        runtime.page.get_by_text("Route Planning").wait_for(state="visible", timeout=10_000)
        _maybe_skip_dialogue(runtime.page, trace)
        _click_button(runtime.page, trace, re.compile(r"Kuro Belt known|Kuro Belt"))
        _press(runtime.page, trace, "Enter")
        try:
            runtime.page.locator(".hud-top-left").filter(has_text="Kuro Belt").wait_for(state="visible", timeout=30_000)
        except Exception:
            runtime.page.wait_for_timeout(500)
        return
    raise ValueError(f"unsupported declared SpaceRPG GUI baseline recipe: {recipe!r}")


def _dock_at_start_station(page: Any, trace: list[dict[str, Any]]) -> None:
    _maybe_skip_dialogue(page, trace)
    try:
        page.locator(".flight-canvas canvas").click(timeout=3_000)
        trace.append({"type": "click", "target": "flight canvas focus"})
    except Exception:
        pass
    _press(page, trace, "KeyF")
    page.locator(".station-screen").wait_for(state="visible", timeout=20_000)
    _maybe_skip_dialogue(page, trace)


def _click_save_slot(page: Any, trace: list[dict[str, Any]], slot_label: str) -> None:
    slot = page.locator(".save-slot", has_text=slot_label).first
    slot.get_by_role("button", name="Save").click(timeout=10_000)
    trace.append({"type": "click", "target": f"save slot {slot_label} Save"})
    page.wait_for_timeout(300)


def _click_button(page: Any, trace: list[dict[str, Any]], name: str | re.Pattern[str]) -> None:
    try:
        page.get_by_role("button", name=name).click(timeout=15_000)
    except Exception:
        _maybe_skip_dialogue(page, trace)
        page.get_by_role("button", name=name).click(timeout=15_000)
    trace.append({"type": "click", "target": f"button:{getattr(name, 'pattern', name)}"})


def _press(page: Any, trace: list[dict[str, Any]], key: str) -> None:
    page.keyboard.press(key)
    trace.append({"type": "keyboard", "key": key})


def _maybe_skip_dialogue(page: Any, trace: list[dict[str, Any]]) -> None:
    deadline = time.perf_counter() + 6
    while time.perf_counter() < deadline:
        clicked = False
        for label in ["Skip", "Next", "Close"]:
            try:
                button = page.get_by_role("button", name=label)
                if button.count() > 0 and button.first.is_visible(timeout=500):
                    button.first.click(timeout=2_000)
                    trace.append({"type": "click", "target": f"button:{label}"})
                    page.wait_for_timeout(150)
                    clicked = True
                    break
            except Exception:
                continue
        try:
            if page.get_by_test_id("dialogue-overlay").count() == 0 and page.get_by_test_id("space-dialogue-overlay").count() == 0:
                return
        except Exception:
            return
        if not clicked:
            page.wait_for_timeout(250)


def _grade_spacerpg_gui_baseline(runtime: BrowserRuntimeAdapter, task: PublicIssue, hidden_spec: dict[str, Any] | None) -> dict[str, Any]:
    page = runtime.page
    snapshot = _visible_snapshot(page)
    save_auto = _read_save(page, "auto")
    save_manual = _read_save(page, "manual-1")
    data = {"visible": snapshot, "save_auto": save_auto, "save_manual_1": save_manual}
    spec = hidden_spec if isinstance(hidden_spec, dict) else {}
    baseline_surface = surface_contract(spec, "gui_from_start_baseline")
    if baseline_surface is None:
        return {"status": "not_reached", "reason": "No GUI grader is defined for this task.", "data": data}
    goal = baseline_surface.get("goal")
    if not isinstance(goal, dict) or not isinstance(goal.get("checks"), list):
        raise ValueError("malformed declared SpaceRPG GUI baseline goal contract")
    checks = evaluate_checks(
        goal["checks"],
        EvaluationContext(spec=spec, current=data),
    )
    reason = _baseline_reason(baseline_surface)
    return _grade(all(check.get("ok") is True for check in checks), data, reason)


def _visible_snapshot(page: Any) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const station = !!document.querySelector(".station-screen");
          const flight = !!document.querySelector(".flight-canvas canvas");
          const galaxy = !!document.querySelector(".galaxy-panel");
          const body = document.body?.innerText ?? "";
          return {
            screen: station ? "station" : galaxy ? "galaxyMap" : flight ? "flight" : "unknown",
            body_text: body.slice(0, 5000)
          };
        }"""
    )


def _read_save(page: Any, slot_id: str) -> dict[str, Any] | None:
    return page.evaluate(
        """(slotId) => {
          const raw = window.localStorage.getItem(`spacerpg-save-slot:${slotId}`);
          if (!raw) return null;
          try { return JSON.parse(raw); } catch { return null; }
        }""",
        slot_id,
    )


def _baseline_requirement(surface: dict[str, Any]) -> dict[str, Any]:
    goal = surface.get("goal")
    requirements = goal.get("requirements") if isinstance(goal, dict) else None
    if (
        not isinstance(requirements, list)
        or len(requirements) != 1
        or not isinstance(requirements[0], dict)
    ):
        raise ValueError("malformed declared SpaceRPG GUI baseline requirement")
    return requirements[0]


def _baseline_recipe(surface: dict[str, Any]) -> str:
    recipe = _baseline_requirement(surface).get("recipe")
    if not isinstance(recipe, str) or not recipe:
        raise ValueError("declared SpaceRPG GUI baseline recipe is missing")
    return recipe


def _baseline_reason(surface: dict[str, Any]) -> str:
    reason = _baseline_requirement(surface).get("reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError("declared SpaceRPG GUI baseline reason is missing")
    return reason


def _grade(ok: bool, data: dict[str, Any], reason: str) -> dict[str, Any]:
    return {"status": "success" if ok else "not_reached", "reason": reason, "data": data}


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)
