"""Task-private live probe for OpenRA PR #21453's hidden ready-building path."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from .agent_s_bridge import AgentSAction, InputBatch
from .c1_openra_runtime import OpenRAGameplaySession
from .lazarus_build import create_prebuild_callback


OPENRA_HOTKEY_PHASE0_TASK_ID = "lz-openra-21453-hotkey-ready-building"
OPENRA_HOTKEY_AUDIT_ENV = "WARPTEST_OPENRA_HOTKEY_AUDIT_PATH"
OPENRA_HOTKEY_AUDIT_FILENAME = "openra-hotkey-ready-building-audit.json"
OPENRA_HOTKEY_STATE_ENV = "WARPTEST_OPENRA_HOTKEY_STATE_PATH"
OPENRA_HOTKEY_STATE_FILENAME = "openra-hotkey-ready-building-state.json"
OPENRA_HOTKEY_OVERLAY_SCHEMA = "c1-lazarus-openra-overlay-v1"
OPENRA_HOTKEY_REGISTERED_START_SHA256 = hashlib.sha256(
    b"c1-lazarus-openra-pr21453-registered-start-v1"
).hexdigest()
OPENRA_HOTKEY_STATE_SEMANTICS_SCHEMA = "c1-lazarus-openra-generated-start-v1"
_TARGET = "warpready"
_QUEUE_TYPE = "Building"
_MIN_PALETTE_ITEMS = 25
_SEMANTIC_START_SELECTOR = "completed_building_placement"
_SEMANTIC_ACTION_TYPE = "openra_build_actor"
_SEMANTIC_TARGET = {"kind": "openra_map", "mod": "ra", "map": "allies-08a"}

_OVERLAY_ROOT = (
    Path(__file__).resolve().parent
    / "instrumentation/openra/lazarus/lz-openra-21453-hotkey-ready-building"
)
_AUDIT_SOURCE = _OVERLAY_ROOT / "WarptestReadyBuildingAudit.cs"
_RULES_PATCH = _OVERLAY_ROOT / "allies-08a-rules.patch"
_MAP_PATCH = _OVERLAY_ROOT / "allies-08a-map.patch"
_AUDIT_TARGET = Path("OpenRA.Mods.Common/Traits/World/WarptestReadyBuildingAudit.cs")
_RULES_TARGET = Path("mods/ra/maps/allies-08a/rules.yaml")
_MAP_TARGET = Path("mods/ra/maps/allies-08a/map.yaml")


@dataclass(frozen=True)
class OpenRAHotkeyAudit:
    """Coordinator-side classification of a real hidden-palette interaction."""

    complete: bool
    goal_checks_ok: bool
    defect_signature_ok: bool
    invariants_ok: bool
    failures: tuple[str, ...]


OpenRAHotkeySessionFactory = Callable[[Path, Path, dict[str, Any]], Any]


def canonicalize_openra_pr21453_checkpoint_semantics(
    generated_spec: Mapping[str, Any],
) -> dict[str, Any]:
    """Project a model checkpoint onto the executable PR #21453 Start.

    The private fixture intentionally owns the hidden completed building, but
    it must not create that Start independently of the state checkpoint.  This
    narrow projection makes the generated map target and a public Buildings
    production action authoritative inputs to the C# restore request.  Any
    other generated shape is rejected before a game is launched.
    """

    if not isinstance(generated_spec, Mapping):
        raise ValueError("OpenRA generated checkpoint must be a mapping")
    target = generated_spec.get("target")
    if not isinstance(target, Mapping):
        raise ValueError("OpenRA generated checkpoint target is missing")
    normalized_target = {
        "kind": target.get("kind"),
        "mod": target.get("mod"),
        "map": target.get("map"),
    }
    if normalized_target != _SEMANTIC_TARGET:
        raise ValueError("OpenRA generated checkpoint target is not the registered map")
    actions = generated_spec.get("actions")
    if not isinstance(actions, list):
        raise ValueError("OpenRA generated checkpoint actions are missing")
    matching_actions = [
        action
        for action in actions
        if isinstance(action, Mapping)
        and action.get("type") == _SEMANTIC_ACTION_TYPE
        and action.get("queue") == _QUEUE_TYPE
    ]
    if len(matching_actions) != 1:
        raise ValueError(
            "OpenRA generated semantic start action must contain exactly one "
            "Buildings production action"
        )
    return {
        "schema": OPENRA_HOTKEY_STATE_SEMANTICS_SCHEMA,
        "target": dict(_SEMANTIC_TARGET),
        "action": {
            "type": _SEMANTIC_ACTION_TYPE,
            "queue": _QUEUE_TYPE,
            "selector": _SEMANTIC_START_SELECTOR,
        },
    }


def registered_openra_pr21453_checkpoint_semantics() -> dict[str, Any]:
    """Return the fixed semantic Start used only by construction-time checks."""

    return canonicalize_openra_pr21453_checkpoint_semantics(
        {
            "target": dict(_SEMANTIC_TARGET),
            "actions": [
                {"type": _SEMANTIC_ACTION_TYPE, "queue": _QUEUE_TYPE}
            ],
        }
    )


def openra_pr21453_checkpoint_semantics_sha256(value: Mapping[str, Any]) -> str:
    """Return the exact semantic-request digest the C# fixture must echo."""

    return _checkpoint_semantics_sha256(_normalize_checkpoint_semantics(value))


def openra_pr21453_hotkey_probe(stage: Literal["before", "after"]) -> dict[str, Any]:
    """Return the real RA gameplay contract for the PR #21453 state.

    The task-private overlay queues ``WARPREADY`` only after the standard
    gameplay setup enables developer all-tech.  The public interaction is a
    normal `E` production-category keypress delivered through OpenRA's existing
    background-pixel input handler, not a coordinator-only widget call.
    """

    if stage not in {"before", "after"}:
        raise ValueError("OpenRA PR #21453 probe stage must be before or after")
    return {
        "name": "OpenRA hidden ready-building production state",
        "benchmark": "openra",
        "timeout_ms": 240_000,
        "setup": {
            "openra_gameplay_probe": {
                "all_tech": True,
                "cash": 50000,
                "fast_build": True,
            }
        },
        "target": {"kind": "openra_map", "mod": "ra", "map": "allies-08a"},
        "checkpoint": {
            "allowed_strategies": ["openra_gameplay_production_validate"],
            "expected_validity": "replay-reachable",
        },
        "actions": [],
        "assertions": [{"type": "no_openra_gameplay_probe_errors"}],
    }


def prepare_openra_pr21453_runtime_overlay(root: str | Path) -> dict[str, str]:
    """Install the exact private RA fixture into one disposable OpenRA clone."""

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise RuntimeError(f"OpenRA overlay root does not exist: {base}")
    if not all(path.is_file() for path in (_AUDIT_SOURCE, _RULES_PATCH, _MAP_PATCH)):
        raise RuntimeError("registered OpenRA PR #21453 overlay assets are missing")
    audit_target = (base / _AUDIT_TARGET).resolve()
    rules_target = (base / _RULES_TARGET).resolve()
    map_target = (base / _MAP_TARGET).resolve()
    if any(base not in target.parents for target in (audit_target, rules_target, map_target)):
        raise RuntimeError("registered OpenRA overlay path escapes its clone")
    if audit_target.exists():
        raise RuntimeError("OpenRA PR #21453 overlay is already installed")
    if not rules_target.is_file() or not map_target.is_file():
        raise RuntimeError("OpenRA PR #21453 map targets are missing")

    _run_git_apply(base, _RULES_PATCH, "--check")
    _run_git_apply(base, _MAP_PATCH, "--check")
    temporary = audit_target.with_name(f".{audit_target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(_AUDIT_SOURCE, temporary)
        _run_git_apply(base, _RULES_PATCH)
        _run_git_apply(base, _MAP_PATCH)
        os.replace(temporary, audit_target)
    finally:
        temporary.unlink(missing_ok=True)

    source_sha = _sha256(_AUDIT_SOURCE)
    rules_patch_sha = _sha256(_RULES_PATCH)
    map_patch_sha = _sha256(_MAP_PATCH)
    audit_sha = _sha256(audit_target)
    rules_sha = _sha256(rules_target)
    map_sha = _sha256(map_target)
    overlay_sha = hashlib.sha256(
        "\n".join(
            (source_sha, rules_patch_sha, map_patch_sha, audit_sha, rules_sha, map_sha)
        ).encode("ascii")
    ).hexdigest()
    return {
        "schema": OPENRA_HOTKEY_OVERLAY_SCHEMA,
        "task_id": OPENRA_HOTKEY_PHASE0_TASK_ID,
        "audit_source_sha256": source_sha,
        "rules_patch_sha256": rules_patch_sha,
        "map_patch_sha256": map_patch_sha,
        "audit_target_sha256": audit_sha,
        "rules_target_sha256": rules_sha,
        "map_target_sha256": map_sha,
        "overlay_sha256": overlay_sha,
    }


def create_openra_pr21453_prebuild_callback(*, timeout_s: float = 300.0):
    """Build a hash-bound PR #21453 version after overlay installation."""

    compile_callback = create_prebuild_callback(
        ("dotnet", "build", "OpenRA.Test/OpenRA.Test.csproj", "-c", "Release", "--no-restore"),
        pre_commands=(
            (
                "dotnet",
                "clean",
                "OpenRA.Test/OpenRA.Test.csproj",
                "-c",
                "Release",
                "--nologo",
            ),
        ),
        artifact_paths=(
            "bin/OpenRA.Game.dll",
            "bin/OpenRA.Mods.Common.dll",
            "bin/OpenRA.Test.dll",
            _AUDIT_TARGET.as_posix(),
            _RULES_TARGET.as_posix(),
            _MAP_TARGET.as_posix(),
        ),
        timeout_s=timeout_s,
    )

    def build(version: str, root: Path) -> dict[str, Any]:
        overlay = prepare_openra_pr21453_runtime_overlay(root)
        compiled = compile_callback(version, root)
        return {
            "build_class": "incremental_dotnet",
            "overlay": overlay,
            "compile": compiled,
        }

    build.lazarus_build_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": "c1-lazarus-openra-pr21453-build-inputs-v1",
                "compile_fingerprint": compile_callback.lazarus_build_fingerprint,
                "audit_source_sha256": _sha256(_AUDIT_SOURCE),
                "rules_patch_sha256": _sha256(_RULES_PATCH),
                "map_patch_sha256": _sha256(_MAP_PATCH),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return build


def evaluate_openra_pr21453_hotkey_audit(value: Mapping[str, Any]) -> OpenRAHotkeyAudit:
    """Fail closed unless the real hidden-ready-building state is evidenced."""

    failures: list[str] = []
    if not isinstance(value, Mapping):
        return _audit_failure(["OpenRA hotkey audit is not a mapping"])
    if value.get("schema_version") != 1:
        failures.append("OpenRA hotkey audit has an unknown schema version")
    if type(value.get("world_tick")) is not int or int(value["world_tick"]) < 1:
        failures.append("OpenRA hotkey audit has no positive world tick")
    if str(value.get("player") or "") != "Greece":
        failures.append("OpenRA hotkey audit has an unexpected local player")
    if str(value.get("queue_type") or "") != _QUEUE_TYPE:
        failures.append("OpenRA hotkey audit has an unexpected production queue")
    if str(value.get("target") or "") != _TARGET:
        failures.append("OpenRA hotkey audit has an unexpected target")
    if str(value.get("queue_item") or "") != _TARGET:
        failures.append("OpenRA hotkey audit has no target at the queue head")
    if value.get("queue_ready") is not True:
        failures.append("OpenRA hotkey audit has no completed target")
    if value.get("target_hidden") is not True:
        failures.append("OpenRA hotkey audit target is visible rather than off-page")
    palette_items = value.get("palette_items")
    if type(palette_items) is not int or palette_items < _MIN_PALETTE_ITEMS:
        failures.append("OpenRA hotkey audit has too few palette items for an off-page target")
    if type(value.get("placement_active")) is not bool:
        failures.append("OpenRA hotkey audit has no placement-mode observation")
    if type(value.get("placement_cancel_observed")) is not bool:
        failures.append("OpenRA hotkey audit has no placement-cancellation observation")
    if failures:
        return _audit_failure(failures)
    placement_active = bool(value["placement_active"])
    return OpenRAHotkeyAudit(
        complete=True,
        goal_checks_ok=placement_active,
        defect_signature_ok=not placement_active,
        invariants_ok=True,
        failures=(),
    )


def run_openra_pr21453_hotkey_probe(
    root: str | Path,
    out_dir: str | Path,
    *,
    timeout_s: float = 120.0,
    session_factory: OpenRAHotkeySessionFactory | None = None,
) -> dict[str, Any]:
    """Exercise PR #21453 through the normal RA ``E`` hotkey route.

    This is a coordinator-side construction probe.  It starts the actual
    background-pixel gameplay session, waits for the private fixture to prepare
    an off-page completed building, sends exactly one ordinary ``E`` key batch,
    and then observes the game's production state.  It never calls the palette
    widget or synthesizes a verdict from the version label.
    """

    repo = Path(root).expanduser().resolve()
    output = Path(out_dir).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"OpenRA PR #21453 probe root does not exist: {repo}")
    if timeout_s <= 0 or timeout_s > 300:
        raise ValueError("OpenRA PR #21453 probe timeout must be in (0, 300]")
    output.mkdir(parents=True, exist_ok=True)
    audit_path = output / OPENRA_HOTKEY_AUDIT_FILENAME
    before_path = output / "openra-pr21453-hotkey-before.png"
    after_path = output / "openra-pr21453-hotkey-after.png"
    factory = session_factory or _default_openra_hotkey_session
    summary: dict[str, Any] = {
        "schema": "c1-lazarus-probe-result-v1",
        "schema_version": 1,
        "benchmark": "openra",
        "case": "hidden-ready-building",
        "kind": "issue_derived_gui_differential",
        "passed": False,
        "status": "failure",
        "before": {"executed": False, "ready": False},
        "after": {"executed": False},
        "input": {"accepted": False},
        "audit_path": str(audit_path),
        "before_path": str(before_path),
        "after_path": str(after_path),
    }
    started = time.monotonic()
    try:
        with _temporary_environment(OPENRA_HOTKEY_AUDIT_ENV, str(audit_path)):
            with factory(repo, output, openra_pr21453_hotkey_probe("after")) as session:
                session.start()
                before_raw = _wait_for_hotkey_audit(
                    audit_path,
                    deadline=started + timeout_s,
                )
                before = evaluate_openra_pr21453_hotkey_audit(before_raw)
                before_ready = before.complete and before_raw.get("placement_active") is False
                summary["before"] = {
                    "executed": True,
                    "ready": before_ready,
                    "audit": _audit_payload(before),
                }
                if not before_ready:
                    summary["status"] = "before_not_ready"
                    return summary
                session.capture_policy_frame(before_path)
                frame_id = getattr(session, "last_policy_frame_id", None)
                if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
                    raise RuntimeError("OpenRA hotkey probe capture returned no frame id")
                batch = InputBatch(
                    session_id=str(getattr(session, "session_id", "")),
                    frame_id=frame_id,
                    batch_id=f"{getattr(session, 'session_id', '')}:{frame_id}:lazarus-openra-pr21453-e",
                    actions=(AgentSAction(kind="key", key="e"),),
                ).validated(width=int(getattr(session, "width", 1280)), height=int(getattr(session, "height", 720)))
                receipt = session.dispatch_input_batch(batch)
                input_ok = _accepted_hotkey_receipt(receipt)
                summary["input"] = _receipt_payload(receipt, accepted=input_ok)
                if not input_ok:
                    summary["status"] = "input_rejected"
                    return summary
                before_tick = int(before_raw["world_tick"])
                after_raw = _wait_for_hotkey_audit(
                    audit_path,
                    deadline=started + timeout_s,
                    minimum_world_tick=before_tick + 1,
                )
                after = evaluate_openra_pr21453_hotkey_audit(after_raw)
                session.capture_policy_frame(after_path)
                summary["after"] = {
                    "executed": True,
                    "audit": _audit_payload(after),
                    "placement_cancel_observed": before_raw.get(
                        "placement_cancel_observed"
                    ),
                    "goal_checks_ok": after.goal_checks_ok,
                    "defect_signature_ok": after.defect_signature_ok,
                    "invariants_ok": after.invariants_ok,
                }
                summary["passed"] = (
                    after.complete and after.goal_checks_ok and after.invariants_ok
                )
                summary["status"] = "success" if summary["passed"] else "goal_not_reached"
                return summary
    except Exception as error:
        summary["status"] = "infrastructure_failure"
        summary["error"] = f"{type(error).__name__}: {error}"
        return summary


def run_openra_pr21453_hotkey_cycle_probe(
    root: str | Path,
    out_dir: str | Path,
    *,
    checkpoint_sha256: str,
    checkpoint_semantics: Mapping[str, Any] | None = None,
    timeout_s: float = 120.0,
    session_factory: OpenRAHotkeySessionFactory | None = None,
) -> dict[str, Any]:
    """Restore the public Start, then repeat the PR #21453 UI path once.

    This is deliberately different from :func:`run_openra_pr21453_hotkey_probe`.
    The latter is the construction-time F2P check and starts before placement.
    This cycle probe records the state-managed Start through the private
    instrumentation, cancels it through an ordinary right-click, and then sends
    the same visible ``E`` production hotkey that exposed the historical bug.
    A caller using this function for Agent-S must *not* set the state environment
    variable; its Phase A instead has to reach the identical Start through UI.
    """

    repo = Path(root).expanduser().resolve()
    output = Path(out_dir).expanduser().resolve()
    if not repo.is_dir():
        raise ValueError(f"OpenRA PR #21453 cycle probe root does not exist: {repo}")
    if not _is_sha256(checkpoint_sha256):
        raise ValueError("OpenRA PR #21453 state checkpoint SHA is invalid")
    if timeout_s <= 0 or timeout_s > 300:
        raise ValueError("OpenRA PR #21453 cycle probe timeout must be in (0, 300]")
    semantics = _normalize_checkpoint_semantics(
        checkpoint_semantics
        if checkpoint_semantics is not None
        else registered_openra_pr21453_checkpoint_semantics()
    )
    semantics_sha256 = _checkpoint_semantics_sha256(semantics)
    output.mkdir(parents=True, exist_ok=True)
    audit_path = output / OPENRA_HOTKEY_AUDIT_FILENAME
    state_path = output / OPENRA_HOTKEY_STATE_FILENAME
    before_path = output / "openra-pr21453-hotkey-cycle-before.png"
    after_path = output / "openra-pr21453-hotkey-cycle-after.png"
    _write_state_restore_request(
        state_path,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_semantics=semantics,
    )
    factory = session_factory or _default_openra_hotkey_session
    summary: dict[str, Any] = {
        "schema": "c1-lazarus-probe-result-v1",
        "schema_version": 1,
        "benchmark": "openra",
        "case": "hidden-ready-building-cycle",
        "kind": "issue_derived_gui_differential",
        "passed": False,
        "status": "failure",
        "before": {
            "executed": False,
            "ready": False,
            "state_restore_applied": False,
        },
        "after": {"executed": False},
        "inputs": [],
        "audit_path": str(audit_path),
        "state_path": str(state_path),
        "before_path": str(before_path),
        "after_path": str(after_path),
        "checkpoint_semantics_sha256": semantics_sha256,
    }
    started = time.monotonic()
    try:
        with _temporary_environment(OPENRA_HOTKEY_AUDIT_ENV, str(audit_path)):
            with _temporary_environment(OPENRA_HOTKEY_STATE_ENV, str(state_path)):
                probe_spec = openra_pr21453_hotkey_probe("after")
                probe_spec["target"] = dict(semantics["target"])
                with factory(repo, output, probe_spec) as session:
                    session.start()
                    before_raw = _wait_for_hotkey_audit(
                        audit_path,
                        deadline=started + timeout_s,
                    )
                    before = evaluate_openra_pr21453_hotkey_audit(before_raw)
                    state_applied = _state_restore_applied(
                        before_raw,
                        checkpoint_sha256=checkpoint_sha256,
                        checkpoint_semantics_sha256=semantics_sha256,
                    )
                    before_ready = (
                        before.complete
                        and state_applied
                        and before_raw.get("placement_active") is True
                    )
                    summary["before"] = {
                        "executed": True,
                        "ready": before_ready,
                        "state_restore_applied": state_applied,
                        "state_restore_spec_sha256": before_raw.get(
                            "state_restore_spec_sha256"
                        ),
                        "audit": _audit_payload(before),
                    }
                    if not before_ready:
                        summary["status"] = "state_restore_not_ready"
                        return summary
                    session.capture_policy_frame(before_path)

                    cancel_receipt = _dispatch_hotkey_action(
                        session,
                        action=AgentSAction(kind="click", x=500, y=350, button="right"),
                        batch_suffix="lazarus-openra-pr21453-cancel",
                        frame_path=output / "openra-pr21453-hotkey-cycle-cancel.png",
                    )
                    cancel_accepted = _accepted_hotkey_receipt(cancel_receipt)
                    summary["inputs"].append(
                        {
                            "kind": "click",
                            "button": "right",
                            "accepted": cancel_accepted,
                        }
                    )
                    if not cancel_accepted:
                        summary["status"] = "cancel_rejected"
                        return summary
                    cancel_raw = _wait_for_hotkey_audit(
                        audit_path,
                        deadline=started + timeout_s,
                        minimum_world_tick=int(before_raw["world_tick"]) + 1,
                    )
                    if (
                        cancel_raw.get("placement_active") is not False
                        or cancel_raw.get("placement_cancel_observed") is not True
                    ):
                        summary["status"] = "placement_not_cancelled"
                        return summary

                    hotkey_receipt = _dispatch_hotkey_action(
                        session,
                        action=AgentSAction(kind="key", key="e"),
                        batch_suffix="lazarus-openra-pr21453-repeat-e",
                        frame_path=output / "openra-pr21453-hotkey-cycle-repeat-e.png",
                    )
                    hotkey_accepted = _accepted_hotkey_receipt(hotkey_receipt)
                    summary["inputs"].append(
                        {
                            "kind": "key",
                            "key": "e",
                            "accepted": hotkey_accepted,
                        }
                    )
                    if not hotkey_accepted:
                        summary["status"] = "hotkey_rejected"
                        return summary
                    after_raw = _wait_for_hotkey_audit(
                        audit_path,
                        deadline=started + timeout_s,
                        minimum_world_tick=int(cancel_raw["world_tick"]) + 1,
                    )
                    after = evaluate_openra_pr21453_hotkey_audit(after_raw)
                    session.capture_policy_frame(after_path)
                    summary["after"] = {
                        "executed": True,
                        "audit": _audit_payload(after),
                        "raw_audit": _coordinator_audit_payload(after_raw),
                        "placement_cancel_observed": after_raw.get(
                            "placement_cancel_observed"
                        ),
                        "goal_checks_ok": after.goal_checks_ok,
                        "defect_signature_ok": after.defect_signature_ok,
                        "invariants_ok": after.invariants_ok,
                    }
                    summary["passed"] = (
                        after.complete and after.goal_checks_ok and after.invariants_ok
                    )
                    summary["status"] = (
                        "success" if summary["passed"] else "goal_not_reached"
                    )
                    return summary
    except Exception as error:
        summary["status"] = "infrastructure_failure"
        summary["error"] = f"{type(error).__name__}: {error}"
        return summary


@contextmanager
def _default_openra_hotkey_session(
    repo: Path, output: Path, spec: dict[str, Any]
) -> Iterator[OpenRAGameplaySession]:
    with OpenRAGameplaySession(
        repo,
        output,
        spec=spec,
        experiment_seed=0,
        bootstrap_timeout_s=120.0,
    ) as session:
        yield session


def _wait_for_hotkey_audit(
    path: Path,
    *,
    deadline: float,
    minimum_world_tick: int | None = None,
) -> dict[str, Any]:
    last_error = "audit file missing"
    while time.monotonic() < deadline:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            last_error = str(error)
            time.sleep(0.05)
            continue
        if not isinstance(value, dict):
            last_error = "audit payload is not an object"
            time.sleep(0.05)
            continue
        audit = evaluate_openra_pr21453_hotkey_audit(value)
        tick = value.get("world_tick")
        if not audit.complete:
            last_error = "; ".join(audit.failures) or "audit is incomplete"
            time.sleep(0.05)
            continue
        if minimum_world_tick is not None and (
            isinstance(tick, bool) or not isinstance(tick, int) or tick < minimum_world_tick
        ):
            last_error = "audit did not advance after the hotkey input"
            time.sleep(0.05)
            continue
        return value
    raise TimeoutError(f"OpenRA PR #21453 audit did not become ready: {last_error}")


def _accepted_hotkey_receipt(value: Any) -> bool:
    return (
        isinstance(value, Mapping)
        and value.get("accepted") is True
        and type(value.get("event_count")) is int
        and int(value["event_count"]) >= 2
    )


def _dispatch_hotkey_action(
    session: Any,
    *,
    action: AgentSAction,
    batch_suffix: str,
    frame_path: Path,
) -> Mapping[str, Any]:
    session.capture_policy_frame(frame_path)
    frame_id = getattr(session, "last_policy_frame_id", None)
    if isinstance(frame_id, bool) or not isinstance(frame_id, int) or frame_id < 0:
        raise RuntimeError("OpenRA hotkey cycle capture returned no frame id")
    batch = InputBatch(
        session_id=str(getattr(session, "session_id", "")),
        frame_id=frame_id,
        batch_id=f"{getattr(session, 'session_id', '')}:{frame_id}:{batch_suffix}",
        actions=(action,),
    ).validated(
        width=int(getattr(session, "width", 1280)),
        height=int(getattr(session, "height", 720)),
    )
    value = session.dispatch_input_batch(batch)
    if not isinstance(value, Mapping):
        raise RuntimeError("OpenRA hotkey cycle input dispatcher returned no mapping")
    return value


def _write_state_restore_request(
    path: Path,
    *,
    checkpoint_sha256: str,
    checkpoint_semantics: Mapping[str, Any],
) -> None:
    semantics = _normalize_checkpoint_semantics(checkpoint_semantics)
    payload = {
        "schema_version": 1,
        "task_id": OPENRA_HOTKEY_PHASE0_TASK_ID,
        "checkpoint_sha256": checkpoint_sha256,
        "semantic_start": semantics,
        "semantic_start_sha256": _checkpoint_semantics_sha256(semantics),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _state_restore_applied(
    value: Mapping[str, Any], *, checkpoint_sha256: str, checkpoint_semantics_sha256: str
) -> bool:
    return (
        value.get("state_restore_requested") is True
        and value.get("state_restore_applied") is True
        and value.get("state_checkpoint_sha256") == checkpoint_sha256
        and value.get("state_restore_spec_sha256") == checkpoint_semantics_sha256
    )


def _normalize_checkpoint_semantics(value: Mapping[str, Any]) -> dict[str, Any]:
    """Reject malformed or hand-edited C# Start requests before execution."""

    if not isinstance(value, Mapping):
        raise ValueError("OpenRA checkpoint semantics must be a mapping")
    target = value.get("target")
    action = value.get("action")
    if (
        value.get("schema") != OPENRA_HOTKEY_STATE_SEMANTICS_SCHEMA
        or not isinstance(target, Mapping)
        or not isinstance(action, Mapping)
        or dict(target) != _SEMANTIC_TARGET
        or action.get("type") != _SEMANTIC_ACTION_TYPE
        or action.get("queue") != _QUEUE_TYPE
        or action.get("selector") != _SEMANTIC_START_SELECTOR
    ):
        raise ValueError("OpenRA checkpoint semantics are not executable")
    if set(value) != {"schema", "target", "action"}:
        raise ValueError("OpenRA checkpoint semantics contain unregistered fields")
    if set(target) != set(_SEMANTIC_TARGET):
        raise ValueError("OpenRA checkpoint semantics target contains unregistered fields")
    if set(action) != {"type", "queue", "selector"}:
        raise ValueError("OpenRA checkpoint semantics action contains unregistered fields")
    return {
        "schema": OPENRA_HOTKEY_STATE_SEMANTICS_SCHEMA,
        "target": dict(_SEMANTIC_TARGET),
        "action": {
            "type": _SEMANTIC_ACTION_TYPE,
            "queue": _QUEUE_TYPE,
            "selector": _SEMANTIC_START_SELECTOR,
        },
    }


def _checkpoint_semantics_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sha256(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _receipt_payload(value: Any, *, accepted: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"accepted": False, "error": "OpenRA input dispatcher returned no mapping"}
    return {
        "accepted": accepted,
        "batch_id": str(value.get("batch_id") or ""),
        "event_count": value.get("event_count"),
        "resulting_frame_id": value.get("resulting_frame_id"),
        "error": str(value.get("error") or ""),
    }


def _audit_payload(value: OpenRAHotkeyAudit) -> dict[str, Any]:
    return {
        "complete": value.complete,
        "goal_checks_ok": value.goal_checks_ok,
        "defect_signature_ok": value.defect_signature_ok,
        "invariants_ok": value.invariants_ok,
        "failures": list(value.failures),
    }


def _coordinator_audit_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the complete trusted audit fields needed by Lazarus group checks.

    The probe's historical convenience booleans remain useful for construction
    reports, but Phase-0 grading projects the registered private groups over
    this raw coordinator payload instead of trusting those pre-computed labels.
    """

    keys = (
        "schema_version",
        "world_tick",
        "player",
        "queue_type",
        "target",
        "queue_item",
        "queue_ready",
        "target_hidden",
        "palette_items",
        "placement_active",
        "placement_cancel_observed",
        "state_restore_requested",
        "state_restore_applied",
        "state_checkpoint_sha256",
        "state_restore_error",
        "probe_errors",
        "state_restore_spec_sha256",
    )
    return {key: value.get(key) for key in keys}


@contextmanager
def _temporary_environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _run_git_apply(base: Path, patch: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "apply", "--whitespace=error", *arguments, str(patch)],
        cwd=base,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "OpenRA PR #21453 runtime overlay failed exact application"
            + (f": {detail}" if detail else "")
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_failure(failures: list[str]) -> OpenRAHotkeyAudit:
    return OpenRAHotkeyAudit(
        complete=False,
        goal_checks_ok=False,
        defect_signature_ok=False,
        invariants_ok=False,
        failures=tuple(dict.fromkeys(failures)),
    )
