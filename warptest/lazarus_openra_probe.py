"""Task-private OpenRA probe and build overlay for the Phase-0 defect."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .lazarus_build import create_prebuild_callback


OPENRA_PHASE0_TASK_ID = "lz-openra-21906-production-tabs"
OPENRA_TABS_AUDIT_ENV = "WARPTEST_OPENRA_TABS_AUDIT_PATH"
OPENRA_TABS_AUDIT_FILENAME = "openra-production-tabs-audit.json"
OPENRA_TABS_CAPTURE_TICKS = 260
OPENRA_OVERLAY_SCHEMA = "c1-lazarus-openra-overlay-v1"

_OVERLAY_ROOT = (
    Path(__file__).resolve().parent
    / "instrumentation/openra/lazarus/lz-openra-21906-production-tabs"
)
_AUDIT_SOURCE = _OVERLAY_ROOT / "WarptestProductionTabsAudit.cs"
_MAP_PATCH = _OVERLAY_ROOT / "gdi01-rules.patch"
_AUDIT_TARGET = Path("OpenRA.Mods.Common/Traits/World/WarptestProductionTabsAudit.cs")
_MAP_TARGET = Path("mods/cnc/maps/gdi01/rules.yaml")
_AUDIT_TICKS = (40, 60, 100, 160, 240)


@dataclass(frozen=True)
class OpenRAProductionTabsAudit:
    """Coordinator-side result of the private production-sidebar observation."""

    complete: bool
    goal_checks_ok: bool
    defect_signature_ok: bool
    invariants_ok: bool
    failures: tuple[str, ...]


def openra_pr21906_probe(stage: Literal["before", "after"]) -> dict[str, Any]:
    """Return the real C&C request that exposes PR #21906's sidebar outcome."""

    if stage not in {"before", "after"}:
        raise ValueError("OpenRA PR #21906 probe stage must be before or after")
    return {
        "name": "OpenRA production sidebar prepared state",
        "benchmark": "openra",
        "timeout_ms": 240_000,
        "setup": {"openra_gameplay_probe": {"all_tech": True}},
        "target": {"kind": "openra_map", "mod": "cnc", "map": "gdi01"},
        "checkpoint": {
            "allowed_strategies": ["openra_gameplay_production_validate"],
            "expected_validity": "replay-reachable",
        },
        "actions": (
            []
            if stage == "before"
            else [{"type": "openra_wait_ticks", "ticks": OPENRA_TABS_CAPTURE_TICKS}]
        ),
        "assertions": [{"type": "no_openra_gameplay_probe_errors"}],
    }


def prepare_openra_pr21906_runtime_overlay(root: str | Path) -> dict[str, str]:
    """Install the exact task-private runtime overlay into one disposable clone.

    The map delta is applied with ``git apply --check`` and then without a
    three-way fallback.  The task's actual inverse patch remains separate: this
    overlay merely recreates the upstream conversation's actor ordering in a
    deterministic C&C map and records the real production-tab state.
    """

    base = Path(root).expanduser().resolve()
    if not base.is_dir():
        raise RuntimeError(f"OpenRA overlay root does not exist: {base}")
    if not _AUDIT_SOURCE.is_file() or not _MAP_PATCH.is_file():
        raise RuntimeError("registered OpenRA Phase-0 overlay assets are missing")
    audit_target = (base / _AUDIT_TARGET).resolve()
    map_target = (base / _MAP_TARGET).resolve()
    if base not in audit_target.parents or base not in map_target.parents:
        raise RuntimeError("registered OpenRA overlay path escapes its clone")
    if audit_target.exists():
        raise RuntimeError("OpenRA PR #21906 overlay is already installed")
    if not map_target.is_file():
        raise RuntimeError("OpenRA PR #21906 map target is missing")

    _run_git_apply(base, "--check")
    temporary = audit_target.with_name(f".{audit_target.name}.{os.getpid()}.tmp")
    try:
        shutil.copyfile(_AUDIT_SOURCE, temporary)
        _run_git_apply(base)
        os.replace(temporary, audit_target)
    finally:
        temporary.unlink(missing_ok=True)

    source_sha = _sha256(_AUDIT_SOURCE)
    patch_sha = _sha256(_MAP_PATCH)
    audit_sha = _sha256(audit_target)
    map_sha = _sha256(map_target)
    overlay_sha = hashlib.sha256(
        "\n".join((source_sha, patch_sha, audit_sha, map_sha)).encode("ascii")
    ).hexdigest()
    return {
        "schema": OPENRA_OVERLAY_SCHEMA,
        "task_id": OPENRA_PHASE0_TASK_ID,
        "audit_source_sha256": source_sha,
        "map_patch_sha256": patch_sha,
        "audit_target_sha256": audit_sha,
        "map_target_sha256": map_sha,
        "overlay_sha256": overlay_sha,
    }


def create_openra_pr21906_prebuild_callback(*, timeout_s: float = 300.0):
    """Build each version after binding the fixed, hashable runtime overlay."""

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
            "bin/OpenRA.Test.dll",
            _AUDIT_TARGET.as_posix(),
            _MAP_TARGET.as_posix(),
        ),
        timeout_s=timeout_s,
    )

    def build(version: str, root: Path) -> dict[str, Any]:
        overlay = prepare_openra_pr21906_runtime_overlay(root)
        compiled = compile_callback(version, root)
        return {
            "build_class": "incremental_dotnet",
            "overlay": overlay,
            "compile": compiled,
        }

    build.lazarus_build_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "schema": "c1-lazarus-openra-pr21906-build-inputs-v1",
                "compile_fingerprint": compile_callback.lazarus_build_fingerprint,
                "audit_source_sha256": _sha256(_AUDIT_SOURCE),
                "map_patch_sha256": _sha256(_MAP_PATCH),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return build


def evaluate_openra_pr21906_audit(value: Mapping[str, Any]) -> OpenRAProductionTabsAudit:
    """Classify the audited live sidebar state without trusting a model verdict.

    The real PR trigger is a non-local production actor being added while a
    local production queue remains disabled.  Once the local prerequisite is
    restored, the fixed sidebar recreates that queue's tab; the exact inverse
    patch leaves it missing.  The event ordering is evidence, not a model
    verdict, so a local-only synthetic sequence is rejected.
    """

    failures: list[str] = []
    if not isinstance(value, Mapping) or not str(value.get("localPlayer") or ""):
        failures.append("OpenRA tabs audit has no local player")
    local_player = str(value.get("localPlayer") or "") if isinstance(value, Mapping) else ""
    non_local_actor = (
        str(value.get("nonLocalProductionActor") or "")
        if isinstance(value, Mapping)
        else ""
    )
    if not non_local_actor or non_local_actor == local_player:
        failures.append("OpenRA tabs audit has no non-local production actor")
    raw_snapshots = value.get("snapshots") if isinstance(value, Mapping) else None
    if not isinstance(raw_snapshots, list) or len(raw_snapshots) != len(_AUDIT_TICKS):
        failures.append("OpenRA tabs audit has an incomplete snapshot sequence")
        return _audit_failure(failures)

    parsed: list[tuple[int, int, int, bool]] = []
    for index, (expected_tick, raw) in enumerate(zip(_AUDIT_TICKS, raw_snapshots)):
        if not isinstance(raw, Mapping):
            failures.append(f"OpenRA tabs audit snapshot {index} is not a mapping")
            continue
        tick = raw.get("worldTick")
        vehicle_tabs = raw.get("vehicleTabs")
        warptab_tabs = raw.get("warptabTabs")
        queues = raw.get("queues")
        if type(tick) is not int or tick != expected_tick:
            failures.append(
                f"OpenRA tabs audit snapshot {index} has unexpected world tick"
            )
        if type(vehicle_tabs) is not int or vehicle_tabs < 0:
            failures.append(
                f"OpenRA tabs audit snapshot {index} has invalid vehicle tab count"
            )
        if type(warptab_tabs) is not int or warptab_tabs < 0:
            failures.append(
                f"OpenRA tabs audit snapshot {index} has invalid task tab count"
            )
        if not isinstance(queues, list) or len(queues) != 1:
            failures.append(
                f"OpenRA tabs audit snapshot {index} must contain one local queue"
            )
            continue
        queue = queues[0]
        if not isinstance(queue, Mapping) or type(queue.get("Enabled")) is not bool:
            failures.append(f"OpenRA tabs audit snapshot {index} has malformed queue state")
            continue
        if (
            str(queue.get("Name") or "") != "warptabdisabled"
            or str(queue.get("Group") or "") != "Vehicle"
            or str(queue.get("Owner") or "") != local_player
        ):
            failures.append(
                f"OpenRA tabs audit snapshot {index} has invalid local queue topology"
            )
        if (
            type(tick) is not int
            or type(vehicle_tabs) is not int
            or type(warptab_tabs) is not int
        ):
            continue
        parsed.append((tick, vehicle_tabs, warptab_tabs, bool(queue["Enabled"])))

    if failures or len(parsed) != len(_AUDIT_TICKS):
        return _audit_failure(failures)
    first_tick, first_vehicle_tabs, first_warptab_tabs, first_enabled = parsed[0]
    post = parsed[1:]
    invariants_ok = (
        first_tick == _AUDIT_TICKS[0]
        and first_vehicle_tabs == 0
        and first_warptab_tabs == 0
        and not first_enabled
        and all(
            vehicle_tabs >= warptab_tabs and enabled
            for _tick, vehicle_tabs, warptab_tabs, enabled in post
        )
    )
    if not invariants_ok:
        failures.append("OpenRA tabs audit trigger-order invariants failed")
        return _audit_failure(failures)
    goal_checks_ok = all(
        vehicle_tabs == 1 and warptab_tabs == 1
        for _tick, vehicle_tabs, warptab_tabs, _enabled in post
    )
    defect_signature_ok = all(
        vehicle_tabs == 0 and warptab_tabs == 0
        for _tick, vehicle_tabs, warptab_tabs, _enabled in post
    )
    return OpenRAProductionTabsAudit(
        complete=True,
        goal_checks_ok=goal_checks_ok,
        defect_signature_ok=defect_signature_ok,
        invariants_ok=True,
        failures=(),
    )


def _run_git_apply(base: Path, *arguments: str) -> None:
    completed = subprocess.run(
        ["git", "apply", "--whitespace=error", *arguments, str(_MAP_PATCH)],
        cwd=base,
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            "OpenRA PR #21906 runtime map overlay failed exact application"
            + (f": {detail}" if detail else "")
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit_failure(failures: list[str]) -> OpenRAProductionTabsAudit:
    return OpenRAProductionTabsAudit(
        complete=False,
        goal_checks_ok=False,
        defect_signature_ok=False,
        invariants_ok=False,
        failures=tuple(dict.fromkeys(failures)),
    )
