"""SpaceRPG C3 fuzz-injection kinds, split out of :mod:`warptest.profiles`.

``SpaceRPGProfile._restore_target`` imports and dispatches into this module lazily
(only when it sees a ``fuzz_*`` target kind), so this module is never part of
the static local-import closure that :mod:`warptest.profiles` contributes to
:func:`warptest.c2_provenance.c2_runtime_input_paths`. C3 state fuzzing
iterates on these injection paths independently of the C2 memory-baseline
comparison; keeping them out of ``profiles.py`` (and excluded via
``c2_provenance._C2_EXCLUDED_RUNTIME_MODULES``) means editing them no longer
invalidates an in-progress C2 harness hash.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .adapters import LocalStorageSaveAdapter
from .models import PhaseResult
from .profiles import ProfileError
from .runtime import BrowserRuntimeAdapter, json_dumps

if TYPE_CHECKING:
    from .profiles import SpaceRPGProfile

FUZZ_KINDS = (
    "fuzz_save_restore",
    "fuzz_legacy_save",
    "fuzz_save_index",
    "fuzz_state_overlay",
    "fuzz_replace_state",
    "fuzz_action_replay",
)

# Persisted SaveGameData surface. Single source of truth:
# warptest.fuzzing.SPACERPG_SEED_KEEP_FIELDS. Kept here as a fallback so direct
# callers that do not pass target["persisted_keys"] (e.g. repro tools) still
# replace only the persisted surface on a full-replace injection.
_SPACERPG_PERSISTED_SAVE_KEYS = (
    "version", "savedAt", "screen", "currentSystemId", "currentStationId", "gameClock",
    "player", "activeMissions", "completedMissionIds", "failedMissionIds", "reputation",
    "factionHeat", "knownSystems", "knownPlanetIds", "explorationState", "dialogueState",
    "onboardingState",
)

_BASE_DISPATCH_KINDS = {"flight", "debug_scenario", "galaxy_map", "station", "equipment_loadout", "state_patch"}


def restore_fuzz_target(
    profile: "SpaceRPGProfile", runtime: BrowserRuntimeAdapter, target: dict[str, Any]
) -> PhaseResult:
    """Dispatch one of the C3 fuzz injection kinds for SpaceRPGProfile._restore_target."""

    kind = target.get("kind")
    if kind == "fuzz_save_restore":
        return _load_via_save(profile, runtime, target, legacy=False, corrupt_index=False)
    if kind == "fuzz_legacy_save":
        return _load_via_save(profile, runtime, target, legacy=True, corrupt_index=False)
    if kind == "fuzz_save_index":
        return _load_via_save(profile, runtime, target, legacy=False, corrupt_index=True)
    if kind == "fuzz_state_overlay":
        return _inject_live_state(profile, runtime, target, replace=False)
    if kind == "fuzz_replace_state":
        return _inject_live_state(profile, runtime, target, replace=True)
    if kind == "fuzz_action_replay":
        return _drive_action_replay(profile, runtime, target)
    return PhaseResult("failure", f"Unsupported SpaceRPG target kind: {kind}")


def _reach_base(profile: "SpaceRPGProfile", runtime: BrowserRuntimeAdapter, base: Any) -> PhaseResult:
    """Drive the store to a renderable base state via the ordinary kind dispatch.

    ``base`` is the spec's original (non-fuzz) target params, e.g.
    ``{"kind": "station", "station_id": "helion-prime"}``. Defaults to a
    playable flight state and never recurses into a fuzz kind.
    """

    if not isinstance(base, dict) or str(base.get("kind", "")).startswith("fuzz_"):
        base = {"kind": "flight"}
    elif base.get("kind") not in _BASE_DISPATCH_KINDS:
        base = {**base, "kind": "flight"}
    return profile._restore_target(runtime, base)


def _save_index_for(slot: str, save: dict[str, Any], *, corrupt: bool = False) -> dict[str, Any]:
    player = save.get("player") if isinstance(save.get("player"), dict) else {}
    summary = {
        "id": slot,
        "label": slot,
        "exists": True,
        "savedAt": save.get("savedAt") or "2020-01-01T00:00:00.000Z",
        "screen": save.get("screen"),
        "currentSystemId": save.get("currentSystemId"),
        "currentStationId": save.get("currentStationId"),
        "credits": player.get("credits"),
        "gameClock": save.get("gameClock"),
        "version": 3,
    }
    index: dict[str, Any] = {"version": 3, "slots": {slot: summary}, "lastPlayedSlotId": slot}
    if corrupt:
        # Keep the index resolvable: version MUST stay supported (an unsupported
        # version makes migrateLegacySave() wipe the whole index, which would just
        # reject every candidate instead of testing index normalization). Poison
        # only non-fatal surroundings so the loader must tolerate a
        # malformed-but-nonfatal index and still resolve to the written slot.
        index["slots"]["__garbage_slot__"] = {"unexpected": True, "exists": "yes"}
        summary["credits"] = "not-a-number"
        summary["gameClock"] = None
        index["extraTopLevelGarbage"] = [1, 2, {"nested": None}]
    return index


def _load_via_save(
    profile: "SpaceRPGProfile",
    runtime: BrowserRuntimeAdapter,
    target: dict[str, Any],
    *,
    legacy: bool,
    corrupt_index: bool,
) -> PhaseResult:
    save = target.get("save")
    if not isinstance(save, dict) or not save:
        return PhaseResult("failure", "SpaceRPG fuzz load kind requires a dict target.save (a SaveGameData).")
    slot = str(target.get("slot", "auto"))
    serialized = json_dumps(save)
    preserve = {"spacerpg-e2e-hook": "enabled", "spacerpg-e2e-disable-economy-backend": "true"}
    if legacy:
        # Legacy migration path: write the deprecated single-save key and clear the
        # index so reload triggers migrateLegacySave() legacy->v3 upgrade.
        values = {**preserve, "spacerpg-save": serialized}
        clear = ["spacerpg-save-index"]
    else:
        values = {
            **preserve,
            f"spacerpg-save-slot:{slot}": serialized,
            "spacerpg-save-index": json_dumps(
                _save_index_for(slot, save, corrupt=corrupt_index)
            ),
        }
        clear = []
    LocalStorageSaveAdapter(runtime).write_many(values, clear, profile.inspect_result.storage_policy)
    runtime.page.reload(wait_until="domcontentloaded")
    runtime.page.wait_for_function(
        "() => typeof window.__SPACERPG_E2E__?.getState === 'function'", timeout=30_000
    )
    # loadGame(slotId?) reads the game's own save; null routes through
    # getLatestSaveSlotId (legacy migration + save-index resolution).
    slot_arg = None if (legacy or corrupt_index) else slot
    loaded = runtime.page.evaluate(
        """(slot) => {
          const store = window.__SPACERPG_E2E__.getState();
          store.stopEconomyStream?.();
          const ok = store.loadGame ? store.loadGame(slot ?? undefined) : false;
          window.__SPACERPG_E2E__.getState().closeDialogue?.();
          return ok === undefined ? true : ok;
        }""",
        slot_arg,
    )
    if not loaded:
        # normalizeSave/migration/index resolution refused the mutated save: the
        # harness could not build this state -> raise so fuzz counts it "rejected"
        # (an unbuildable input, not a robustness defect).
        path = "legacy migration" if legacy else ("save index" if corrupt_index else "save slot")
        raise ProfileError(f"SpaceRPG loadGame rejected the mutated save via {path} (normalizeSave/index refused it).")
    detail = (
        "Loaded mutated save through legacy migration path."
        if legacy
        else ("Loaded mutated save through corrupt-index resolution." if corrupt_index else "Loaded mutated save through slot loadGame().")
    )
    return PhaseResult("success", detail, {"validity": "save-restorable"})


def _inject_live_state(
    profile: "SpaceRPGProfile", runtime: BrowserRuntimeAdapter, target: dict[str, Any], *, replace: bool
) -> PhaseResult:
    save = target.get("save")
    if not isinstance(save, dict) or not save:
        return PhaseResult("failure", "SpaceRPG fuzz live-state kind requires a dict target.save.")
    base_result = _reach_base(profile, runtime, target.get("base"))
    if base_result.status != "success":
        return base_result
    persisted_keys = target.get("persisted_keys")
    if not isinstance(persisted_keys, list) or not persisted_keys:
        persisted_keys = list(_SPACERPG_PERSISTED_SAVE_KEYS)
    # Call store cleanup actions BEFORE the write: they must run while the
    # store still holds its action functions.
    runtime.page.evaluate(
        """({ state, replace, persistedKeys }) => {
          const hook = window.__SPACERPG_E2E__;
          const cur = hook.getState();
          cur.stopEconomyStream?.();
          cur.closeDialogue?.();
          if (replace === true) {
            // Replace only the persisted save surface; preserve action methods
            // and non-persisted structural/runtime fields so the injection tests
            // a fully-replaced persisted state, not a store stripped of methods
            // (which would crash the app unconditionally regardless of mutation).
            const next = { ...hook.getState() };
            for (const k of persistedKeys) delete next[k];
            Object.assign(next, state);
            hook.setState(next, true);
          } else {
            hook.setState(state, false);
          }
        }""",
        {"state": save, "replace": replace, "persistedKeys": persisted_keys},
    )
    kind_label = "persisted-surface replace (methods+structural preserved)" if replace else "shallow-merge overlay"
    return PhaseResult("success", f"Applied mutated save via setState {kind_label}.", {"validity": "injection-only"})


def _drive_action_replay(profile: "SpaceRPGProfile", runtime: BrowserRuntimeAdapter, target: dict[str, Any]) -> PhaseResult:
    actions = target.get("actions")
    if not isinstance(actions, list) or not actions:
        return PhaseResult("failure", "SpaceRPG fuzz_action_replay requires a non-empty target.actions list.")
    base_result = _reach_base(profile, runtime, target.get("base"))
    if base_result.status != "success":
        return base_result
    # Drive a sequence of adversarial store actions. Per-action throws are
    # swallowed in JS so one bad reducer does not abort the sequence; the
    # resulting frame (blank/crashed/soft-locked) is what the verdict inspects.
    drive = runtime.page.evaluate(
        """(actions) => {
          const hook = window.__SPACERPG_E2E__;
          let invoked = 0;
          for (const action of actions) {
            if (!action || typeof action !== 'object') continue;
            const store = hook.getState();
            const fn = store[action.method];
            if (typeof fn !== 'function') continue;
            const args = Array.isArray(action.args) ? action.args : [];
            try { fn(...args); invoked += 1; }
            catch (e) { /* adversarial action threw; keep going */ }
          }
          hook.getState().closeDialogue?.();
          return invoked;
        }""",
        actions,
    )
    return PhaseResult("success", f"Drove {drive} adversarial store action(s).", {"validity": "injection-only"})
