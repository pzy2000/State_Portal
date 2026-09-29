#!/usr/bin/env python3
"""Generate persistent before/after golden screenshots for core benchmark samples.

For each selected core task (from ``benchmarks/core/hidden/oracles.yaml``) this
runs the hidden spec twice on the live runtime:

- before: the restored target with no smoke actions (optionally a task-specific
  before action list, e.g. to dismiss a dialogue or open a menu).
- after: the original spec actions (optionally with extra trailing actions).

Both frames are written into the spec's ``visual/`` directory as
``<task-id>.before.golden.png`` / ``<task-id>.after.golden.png``. The existing
single ``golden_image`` is left untouched; wiring ``golden_before``/``golden_after``
into the spec YAML is a separate step.

Two capture modes are supported per benchmark:
- ``run``: the profile screenshots inside run() for profiles whose registered
  visual capture has no DOM preparation.
- ``visual``: screenshots through the visual-oracle path. Browser profiles that
  declare ``capture.hide_selectors`` must use this path so golden regeneration and
  formal live capture apply exactly the same overlay-hiding rules. We run under the
  ``fake-visual`` policy (no LLM) with ``visual_on_semantic_failure`` so a frame is
  always captured.

Usage:
    python3 scripts/generate_visual_pairs.py --benchmark 2048 DatingSim
    python3 scripts/generate_visual_pairs.py --benchmark SpaceRPG --task spacerpg-galaxy-jump
    python3 scripts/generate_visual_pairs.py --list
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import yaml
from PIL import Image, ImageChops, ImageStat

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from warptest.c1_catalog import canonical_benchmark_id  # noqa: E402
from warptest.c1_evaluation import (  # noqa: E402
    materialize_legacy_spec,
    visual_oracle as evaluation_visual_oracle,
)
from warptest.runner import run_spec  # noqa: E402
from warptest.task_schema import load_hidden_oracles, resolve_hidden_spec  # noqa: E402
from warptest.visual import screenshot_quality  # noqa: E402

CLOSE_DIALOGUE = {"type": "hook_call", "method": "closeDialogue"}

# Elevated 3/4 establishing shot of the synthesized cobble/wood house, framed so the
# whole structure (walls + roof + doorway) is visible instead of the default spawn
# that lands the player embedded inside the walls (a black frame). Generation-only so
# the headless semantic path (world-only assertions) is not forced onto the GUI client.
LUANTI_HOUSE_VANTAGE = {
    "type": "luanti_set_player_pos",
    "pos": {"x": -7, "y": 9, "z": -7},
    "hover": True,
    "look_at": {"x": 3, "y": 3, "z": 4},
}

# Center the dig-node target block (3,2,3) in frame so the before/after contrast is
# obvious: before shows the cobble squarely, after shows that spot emptied (with the
# dug cobble now in the hotbar). Generation-only camera; the semantic path is untouched.
LUANTI_DIG_VANTAGE = {
    "type": "luanti_set_player_pos",
    "pos": {"x": 0, "y": 3, "z": 0},
    "hover": True,
    "look_at": {"x": 3, "y": 2, "z": 3},
}

LUANTI_HOMESTEAD_VANTAGE = {
    "type": "luanti_set_player_pos",
    "pos": {"x": -7, "y": 8, "z": -7},
    "hover": True,
    "look_at": {"x": 3, "y": 2.5, "z": 3.5},
}

LUANTI_WORKSHOP_VANTAGE = {
    "type": "luanti_set_player_pos",
    "pos": {"x": -7, "y": 6, "z": 9},
    "hover": True,
    "look_at": {"x": 3, "y": 2.5, "z": 3},
}

# The task's graded final position is on the far side of the build and leaves the
# inherited camera pointed over an empty gravel plane.  Capture from an elevated
# diagonal position instead so the generated timber bridge, cobble wall, stair,
# and destination light are all inside the frame.  This is generation-only and
# does not change the semantic player-position assertion.
LUANTI_MINE_OUTPOST_VANTAGE = {
    "type": "luanti_set_player_pos",
    "pos": {"x": -5, "y": 8, "z": -5},
    "hover": True,
    "look_at": {"x": 5, "y": 2.5, "z": 6},
}

# Frame the 3x3 stone basin at (6..8,1,0..2) so the empty basin and the placed
# water source at (7,2,1) occupy the same screen region in both frames. The
# vantage is applied twice around a wait: a single teleport is corrected by the
# client's own movement prediction often enough that the two frames otherwise
# end up at different camera heights.
# A restored Luanti world keeps whatever the previous run left behind, so water
# placed by an earlier after-pass is still flowing across the basin when the next
# before-pass starts. Clearing everything above the basin floor makes each frame
# show exactly the declared precondition.
LUANTI_WATER_BASIN_CLEANUP = [
    {
        "type": "luanti_fill",
        "from": {"x": 0, "y": 2, "z": -4},
        "to": {"x": 14, "y": 5, "z": 10},
        "node": "air",
    },
    {"type": "wait", "ms": 300},
]

# A hovering vantage is not reproducible here: the client keeps predicting its own
# vertical movement, so the two passes end up at different camera heights. Dropping
# the player onto the plane and only then aiming from the settled position frames
# the basin the same way every time.
LUANTI_WATER_BASIN_VANTAGE = [
    {
        "type": "luanti_set_player_pos",
        "pos": {"x": 7, "y": 4, "z": -5},
    },
    {"type": "wait", "ms": 900},
    {
        "type": "luanti_set_player_look",
        "look_at": {"x": 7, "y": 1.6, "z": 1},
    },
    {"type": "wait", "ms": 300},
]

# Docking at a SpaceRPG station already creates an auto save. The Hangar tab renders
# that slot's timestamp, so the station-save pair must cross a minute boundary
# before the explicit save; otherwise the before/after text is pixel-identical
# even though the persisted save fingerprint changed.
SPACERPG_STATION_HANGAR_TAB = [
    {"type": "hook_call", "method": "setStationTab", "args": ["Hangar"]},
    {"type": "wait", "ms": 400},
]

# A PAL3 scene load or save restore finishes with a camera fade, and the capture
# that lands one second later is still inside that fade: the historical q02 after
# goldens are pure black for exactly this reason. The instrumentation caps a single
# wait at five seconds, so the settle is expressed as a chain.
PAL3_SCENE_SETTLE = [
    {"type": "wait", "ms": 5000},
    {"type": "wait", "ms": 5000},
]

# PAL3 shows neither money nor inventory anywhere in the field view, so the
# surface a task's after frame lands on is what decides whether the frame carries
# that task's own evidence. The world map draws one button per unlocked region and
# is where the region tasks differ; TeamOpen spawns the non-player team members as
# visible actors beside the player and is where the party tasks differ. All three
# vantages are generation-only and run after the graded actions, so the semantic
# contract is untouched.
PAL3_WORLD_MAP_VANTAGE = [
    *PAL3_SCENE_SETTLE,
    {"type": "pal3_toggle_world_map"},
    {"type": "wait", "ms": 1500},
]
PAL3_MAIN_MENU_VANTAGE = [
    *PAL3_SCENE_SETTLE,
    {"type": "pal3_toggle_main_menu"},
    {"type": "wait", "ms": 1500},
]
PAL3_TEAM_VANTAGE = [
    *PAL3_SCENE_SETTLE,
    {"type": "pal3_team_open"},
    {"type": "wait", "ms": 1500},
]
# The two region tasks unlock the same travelable region, so the map alone draws
# the same single button for both. They do recruit different companions, so the
# party is spawned first and the map opened over it: the map panel is small and
# the characters stay visible beside it.
PAL3_TEAM_WORLD_MAP_VANTAGE = [
    *PAL3_SCENE_SETTLE,
    {"type": "pal3_team_open"},
    {"type": "wait", "ms": 1500},
    {"type": "pal3_toggle_world_map"},
    {"type": "wait", "ms": 1500},
]

# Per-benchmark capture configuration. ``capture`` selects how the screenshot is
# obtained; ``repo`` is the game repository the runtime launches.
BENCHMARKS: dict[str, dict[str, Any]] = {
    "2048": {"repo": "games/2048", "capture": "visual"},
    "datingsim": {"repo": "games/DatingSim", "capture": "visual"},
    "spacerpg": {"repo": "games/SpaceRPG", "capture": "visual"},
    "luanti": {"repo": "games/luanti", "capture": "visual"},
    "openra": {"repo": "games/OpenRA", "capture": "visual"},
    "jupyterlab": {
        "repo": "games/jupyterlab",
        "capture": "run",
        "update_single_golden_from_after": True,
    },
    "libreoffice": {
        "repo": "games/LibreOffice",
        "capture": "libreoffice_state",
        "update_single_golden_from_after": True,
    },
    "blender": {
        "repo": "games/blender",
        "capture": "run",
        "update_single_golden_from_after": True,
    },
    "jynew": {"repo": "games/jynew", "capture": "visual"},
    "pal3": {"repo": "games/Pal3.Unity", "capture": "visual"},
}

# Per-task capture overrides keyed by task id. ``before_actions`` replaces the empty
# default before-action list; ``after_extra_actions`` is appended to the spec actions
# for the after frame; ``regen_before`` / ``regen_after`` gate which frame is rewritten.
# ``require_changed_pixel_ratio`` rejects a pair whose task-relevant region did not
# actually move: whole-frame mean difference is dominated by unchanged chrome, so a
# small but real change (a stat bar, a save slot row, a merged tile) is only visible
# as a fraction of clearly-changed pixels.
TASK_OVERRIDES: dict[str, dict[str, Any]] = {
    "spacerpg-trade-refit-mining-sortie": {
        "regen_before": False,
        "require_rendered_flight_scene": True,
    },
    "spacerpg-station-save": {
        "before_actions": list(SPACERPG_STATION_HANGAR_TAB),
        "after_prefix_actions": [{"type": "wait", "ms": 65000}],
        "after_extra_actions": list(SPACERPG_STATION_HANGAR_TAB),
        "require_changed_pixel_ratio": 0.0001,
    },
    "2048-merge-right": {"require_changed_pixel_ratio": 0.005},
    "2048-merge-up": {"require_changed_pixel_ratio": 0.005},
    "2048-merge-down": {"require_changed_pixel_ratio": 0.005},
    "2048-double-merge-left": {"require_changed_pixel_ratio": 0.005},
    # The Love gameplay screen only moves its stat bars and their numeric labels,
    # so the achievable ratio is an order of magnitude below a board or scene
    # change; the gate still rejects an unchanged or duplicated frame.
    "love-gym-workout": {"require_changed_pixel_ratio": 0.0008},
    "love-shopping-mall-spa": {"require_changed_pixel_ratio": 0.0008},
    "love-office-work-coding": {"require_changed_pixel_ratio": 0.0008},
    "luanti-water-source": {
        "before_actions": [*LUANTI_WATER_BASIN_CLEANUP, *LUANTI_WATER_BASIN_VANTAGE],
        "after_prefix_actions": list(LUANTI_WATER_BASIN_CLEANUP),
        "after_extra_actions": list(LUANTI_WATER_BASIN_VANTAGE),
        "require_changed_pixel_ratio": 0.01,
    },
    "spacerpg-trading-cargo": {
        "before_actions": [CLOSE_DIALOGUE],
        "after_extra_actions": [CLOSE_DIALOGUE],
    },
    "spacerpg-equipment-loadout": {
        "before_actions": [CLOSE_DIALOGUE],
        "after_extra_actions": [CLOSE_DIALOGUE],
    },
    "spacerpg-save-restore": {
        "before_actions": [
            {"type": "hook_call", "method": "saveGame", "args": ["manual-1"]},
            CLOSE_DIALOGUE,
            {"type": "hook_call", "method": "setScreen", "args": ["pause"]},
        ],
        "regen_after": False,
    },
    "love-elena-dialogue-memory-consolidation": {
        "before_actions": [{"type": "wait", "ms": 1600}],
    },
    "luanti-complex-house-workshop": {
        "before_actions": [LUANTI_HOUSE_VANTAGE],
        "after_extra_actions": [LUANTI_HOUSE_VANTAGE],
    },
    "luanti-dig-node": {
        "before_actions": [LUANTI_DIG_VANTAGE],
        "after_extra_actions": [LUANTI_DIG_VANTAGE],
    },
    "luanti-self-sufficient-homestead-roundtrip": {
        "before_actions": [LUANTI_HOMESTEAD_VANTAGE],
        "after_extra_actions": [LUANTI_HOMESTEAD_VANTAGE],
    },
    "luanti-workshop-order-staging": {
        "before_actions": [LUANTI_WORKSHOP_VANTAGE],
        "after_extra_actions": [LUANTI_WORKSHOP_VANTAGE],
    },
    "luanti-mine-bridge-outpost-build": {
        "before_actions": [LUANTI_MINE_OUTPOST_VANTAGE],
        "after_extra_actions": [LUANTI_MINE_OUTPOST_VANTAGE],
        "require_changed_pixel_ratio": 0.015,
    },
    "jynew-sidequest-trade-submap": {
        "require_visible_change": True,
    },
    # All five PAL3 tasks end in the same q02 courtyard, so the after frame is taken
    # on the surface that carries that task's own assertions. The two region tasks
    # unlock the same region, so the map alone cannot separate them and the party is
    # spawned as well; they differ by one recruited actor. The two save roundtrips
    # likewise differ only by party size. The scene-transition task asserts the scene
    # itself, so it keeps the plain scene view.
    "pal3-scene-transition": {
        "before_actions": list(PAL3_SCENE_SETTLE),
        "after_extra_actions": list(PAL3_SCENE_SETTLE),
        "require_changed_pixel_ratio": 0.05,
    },
    # A save roundtrip that keeps a one-actor party changes nothing on screen, so
    # this pair carries no ratio gate: the audit records the measured delta as
    # weak-visual-delta instead of a threshold pretending to be evidence. The team
    # vantage is still applied so the frame is comparable with the companion task,
    # whose party does grow.
    "pal3-scene-restore": {
        "before_actions": list(PAL3_SCENE_SETTLE),
        "after_extra_actions": list(PAL3_TEAM_VANTAGE),
    },
    "pal3-story-party-scene-roundtrip": {
        "before_actions": list(PAL3_SCENE_SETTLE),
        "after_extra_actions": list(PAL3_TEAM_WORLD_MAP_VANTAGE),
        "require_changed_pixel_ratio": 0.05,
    },
    "pal3-world-region-travel-roundtrip": {
        "before_actions": list(PAL3_SCENE_SETTLE),
        "after_extra_actions": list(PAL3_TEAM_WORLD_MAP_VANTAGE),
        "require_changed_pixel_ratio": 0.05,
    },
    # The whole visible delta here is the one recruited companion standing beside
    # the player, which measures 0.0055 of the frame. The gate sits between that
    # and the 0.0002 a party that did not grow leaves behind, so a silently failed
    # recruitment cannot be shipped as evidence.
    "pal3-companion-ending-preparation": {
        "before_actions": list(PAL3_SCENE_SETTLE),
        "after_extra_actions": list(PAL3_TEAM_VANTAGE),
        "require_changed_pixel_ratio": 0.002,
    },
    "jupyterlab-reproducible-experiment-workspace": {
        "require_visible_change": True,
    },
}


def _abs_visual_oracle(spec: dict[str, Any], spec_path: Path) -> None:
    """Make golden_image absolute and drop the pair keys for temp-spec validation."""
    vo = evaluation_visual_oracle(spec)
    if not isinstance(vo, dict):
        return
    golden = vo.get("golden_image")
    if isinstance(golden, str) and golden:
        p = Path(golden)
        if not p.is_absolute():
            p = (spec_path.parent / p)
        if not p.exists():
            anchors = sorted((spec_path.parent / "visual").glob("*.golden.png"))
            if not anchors:
                raise FileNotFoundError(
                    f"No existing visual anchor is available while bootstrapping {spec_path}"
                )
            p = anchors[0]
        vo["golden_image"] = str(p.resolve())
    # The pair goldens may not exist yet; drop them so validation does not fail.
    vo.pop("golden_before", None)
    vo.pop("golden_after", None)


def _capture_once(
    spec: dict[str, Any],
    *,
    capture: str,
    repo: Path,
    src_spec_path: Path,
    stem: str,
    headless: bool,
    work_dir: Path,
) -> Path:
    spec_path = work_dir / f"{stem}.yaml"
    report_dir = work_dir / f"{stem}-run"
    spec = copy.deepcopy(spec)
    if capture == "libreoffice_state":
        from warptest.c1_desktop_runtime import (  # noqa: PLC0415
            LibreOfficeC1DesktopSession,
            semantic_probe_passed,
        )

        report_dir.mkdir(parents=True, exist_ok=True)
        shot = report_dir / f"{stem}.png"
        with LibreOfficeC1DesktopSession(repo, report_dir) as session:
            restored = session.restore_target(spec)
            if restored.get("status") != "success":
                raise RuntimeError(
                    f"{stem}: LibreOffice target restore failed: {restored}"
                )
            start_probe = session.probe_start(spec)
            if not semantic_probe_passed(
                start_probe, expected_source=session.semantic_source
            ):
                raise RuntimeError(
                    f"{stem}: LibreOffice start probe failed: {start_probe}"
                )
            label = "after" if spec.get("actions") else "before"
            semantic_evidence = start_probe
            if spec.get("actions"):
                goal_probe = session.execute_state_transition(spec)
                if not semantic_probe_passed(
                    goal_probe, expected_source=session.semantic_source
                ):
                    raise RuntimeError(
                        f"{stem}: LibreOffice goal probe failed: {goal_probe}"
                    )
                semantic_evidence = goal_probe
            prepared = session.prepare_visual_capture(spec, label)
            if (
                prepared.get("document_identity")
                != semantic_evidence.get("document_identity")
            ):
                raise RuntimeError(
                    f"{stem}: LibreOffice visual document identity does not match "
                    "the semantically graded document"
                )
            capture_options = (
                (evaluation_visual_oracle(spec) or {}).get("capture") or {}
            )
            settle_ms = int(capture_options.get("wait_ms", 500))
            if settle_ms > 0:
                time.sleep(settle_ms / 1000.0)
            session.capture_policy_frame(shot)
            _validate_libreoffice_capture_metadata(
                stem,
                str((spec.get("target") or {}).get("kind", "")),
                session.last_policy_frame_capture,
            )
        return shot
    if capture == "run":
        # The profile screenshots in run(); the visual_oracle block (with its
        # relative golden path) is unnecessary and would fail temp-spec validation.
        spec.pop("visual_oracle", None)
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
        report = run_spec(spec_path, repo_arg=str(repo), report_dir=report_dir, headless=headless, oracle_policy="semantic")
    else:
        # Keep visual_oracle (golden_image absolutized) so the fake-visual audit
        # captures a frame even when the no-action before state fails semantics.
        _abs_visual_oracle(spec, src_spec_path)
        spec_path.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
        report = run_spec(
            spec_path,
            repo_arg=str(repo),
            report_dir=report_dir,
            headless=headless,
            oracle_policy="fake-visual",
            visual_on_semantic_failure=True,
        )
    screenshot_candidates = _preferred_capture_candidates(report, capture=capture)
    # Engine profiles can persist their native post-smoke frame even when the
    # visual-judge layer rejects or skips the pair (for example while the
    # after golden is being bootstrapped).  That frame is still the real
    # deterministic reference replay and is the correct bootstrap source.
    if not screenshot_candidates:
        screenshot_candidates = sorted(report_dir.glob("*.png"))
    informative = [
        path for path in screenshot_candidates
        if path.exists() and screenshot_quality(path)["informative"]
    ]
    if not informative:
        raise RuntimeError(f"{stem}: no informative screenshot (restore={report.checkpoint_restoration.status}).")
    return Path(informative[0])


def _preferred_capture_candidates(report: Any, *, capture: str) -> list[Path]:
    """Return the frame produced by the requested capture path first.

    Browser profiles save an ordinary post-smoke screenshot before the visual
    oracle injects ``hide_selectors`` and captures its policy frame.  Selecting
    ``report.screenshots[0]`` therefore silently bypassed the visual path even
    when the caller requested it.
    """

    if capture == "visual":
        result = getattr(report, "visual_oracle_result", None)
        data = getattr(result, "data", None)
        audit = data.get("visual_audit") if isinstance(data, dict) else None
        frame_paths = audit.get("frame_paths") if isinstance(audit, dict) else None
        if isinstance(frame_paths, list) and frame_paths:
            return [Path(path) for path in frame_paths if isinstance(path, str)]
    return [Path(path) for path in getattr(report, "screenshots", [])]


def _validate_libreoffice_capture_metadata(
    stem: str,
    target_kind: str,
    metadata: dict[str, Any] | None,
) -> None:
    if not isinstance(metadata, dict):
        raise RuntimeError(f"{stem}: LibreOffice capture metadata is missing")
    title = metadata.get("active_window_title")
    if not isinstance(title, str) or not title.strip():
        raise RuntimeError(
            f"{stem}: LibreOffice active window title is missing"
        )
    normalized = title.casefold()
    if target_kind not in {
        "writer_document",
        "calc_spreadsheet",
        "impress_presentation",
    }:
        raise RuntimeError(
            f"{stem}: unsupported LibreOffice visual kind {target_kind!r}"
        )
    if any(
        marker in normalized
        for marker in ("start center", "startcenter", "welcome to libreoffice")
    ) or normalized in {"libreoffice", "libreofficedev"}:
        raise RuntimeError(
            f"{stem}: LibreOffice Start Center cannot be a visual golden"
        )


def _assert_libreoffice_pair_is_distinct(
    task_id: str, before: Path, after: Path
) -> None:
    before_sha = hashlib.sha256(before.read_bytes()).hexdigest()
    after_sha = hashlib.sha256(after.read_bytes()).hexdigest()
    if before_sha == after_sha:
        raise ValueError(
            f"{task_id}: LibreOffice before/after frames are byte-identical "
            f"({before_sha})"
        )


def _assert_pair_has_visible_change(
    task_id: str,
    before: Path,
    after: Path,
    *,
    minimum_mean_absolute_difference: float = 2.0,
) -> None:
    """Reject near-identical frames while ignoring a volatile top overlay.

    Unity's FPS counter and small ambient animations can change the PNG digest
    even when the actual task scene has not changed. Cropping the top 15% and
    comparing mean absolute RGB difference makes the sidequest's map transition
    the evidence under test instead of incidental animation noise.
    """
    with Image.open(before) as before_image, Image.open(after) as after_image:
        before_rgb = before_image.convert("RGB")
        after_rgb = after_image.convert("RGB")
        if before_rgb.size != after_rgb.size:
            raise ValueError(
                f"{task_id}: before/after frame dimensions differ: "
                f"{before_rgb.size} != {after_rgb.size}"
            )
        width, height = before_rgb.size
        crop_top = round(height * 0.15)
        crop_box = (0, crop_top, width, height)
        difference = ImageChops.difference(
            before_rgb.crop(crop_box),
            after_rgb.crop(crop_box),
        )
        channel_means = ImageStat.Stat(difference).mean
        mean_absolute_difference = sum(channel_means) / len(channel_means)

    if mean_absolute_difference < minimum_mean_absolute_difference:
        raise ValueError(
            f"{task_id}: before/after frames have no meaningful visible change "
            f"(mean absolute RGB difference "
            f"{mean_absolute_difference:.3f} < "
            f"{minimum_mean_absolute_difference:.3f})"
        )


_CHANGED_PIXEL_LUMA_THRESHOLD = 24


def _pair_change_metrics(before: Path, after: Path) -> dict[str, float]:
    """Whole-frame mean difference plus the fraction of clearly-changed pixels.

    Mean difference is dominated by the unchanged chrome that surrounds the task
    region, so a real but compact change (a stat bar, a save-slot row, a merged
    tile) reads as near-zero. The changed-pixel ratio keeps that change visible
    to the gate.
    """
    with Image.open(before) as before_image, Image.open(after) as after_image:
        before_rgb = before_image.convert("RGB")
        after_rgb = after_image.convert("RGB")
        if before_rgb.size != after_rgb.size:
            raise ValueError(
                f"before/after frame dimensions differ: "
                f"{before_rgb.size} != {after_rgb.size}"
            )
        difference = ImageChops.difference(before_rgb, after_rgb)
        channel_means = ImageStat.Stat(difference).mean
        histogram = difference.convert("L").histogram()
    total = sum(histogram) or 1
    changed = sum(histogram[_CHANGED_PIXEL_LUMA_THRESHOLD:])
    return {
        "mean_absolute_difference": sum(channel_means) / len(channel_means),
        "changed_pixel_ratio": changed / total,
    }


def _assert_pair_has_changed_region(
    task_id: str,
    before: Path,
    after: Path,
    *,
    minimum_changed_pixel_ratio: float,
) -> dict[str, float]:
    metrics = _pair_change_metrics(before, after)
    if metrics["changed_pixel_ratio"] < minimum_changed_pixel_ratio:
        raise ValueError(
            f"{task_id}: before/after frames do not show a task-specific change "
            f"(changed pixel ratio {metrics['changed_pixel_ratio']:.5f} < "
            f"{minimum_changed_pixel_ratio:.5f}, mean absolute RGB difference "
            f"{metrics['mean_absolute_difference']:.3f})"
        )
    return metrics


NEAR_DUPLICATE_CHANGED_PIXEL_RATIO = 0.0005


def frames_are_near_duplicates(first: Path, second: Path) -> bool:
    """Whether two frames are the same picture to a reader, not just to sha256.

    Two PAL3 after frames once differed in a handful of bytes (an FPS counter, a
    memory readout) while showing an identical scene, so a digest comparison
    reported them as distinct evidence and a reader could not tell the tasks
    apart. The threshold remains below Love's smallest nameable resource/time
    delta (0.00083 of the frame), while still rejecting identical or counter-only
    frames.
    """
    with Image.open(first) as first_image, Image.open(second) as second_image:
        if first_image.size != second_image.size:
            return False
    metrics = _pair_change_metrics(first, second)
    return metrics["changed_pixel_ratio"] < NEAR_DUPLICATE_CHANGED_PIXEL_RATIO


def _assert_spacerpg_flight_scene_is_rendered(
    task_id: str,
    image_path: Path,
    *,
    minimum_luma_stddev: float = 8.0,
) -> None:
    """Reject a SpaceRPG frame whose central WebGL scene is still effectively black."""
    with Image.open(image_path) as image:
        rgb = image.convert("RGB")
        width, height = rgb.size
        scene = rgb.crop(
            (
                round(width * 0.34),
                round(height * 0.22),
                round(width * 0.66),
                round(height * 0.72),
            )
        ).convert("L")
        luma_stddev = ImageStat.Stat(scene).stddev[0]

    if luma_stddev < minimum_luma_stddev:
        raise ValueError(
            f"{task_id}: central SpaceRPG flight scene appears unrendered "
            f"(luma stddev {luma_stddev:.3f} < {minimum_luma_stddev:.3f})"
        )


def _capture(
    spec: dict[str, Any],
    *,
    capture: str,
    repo: Path,
    src_spec_path: Path,
    stem: str,
    headless: bool,
    wait_ms: int,
    work_dir: Path,
    retries: int,
) -> Path:
    # Engine runtimes (Unity/xvfb) occasionally miss a frame; retry a few times.
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            shot = _capture_once(
                spec, capture=capture, repo=repo, src_spec_path=src_spec_path,
                stem=f"{stem}-try{attempt + 1}" if attempt else stem, headless=headless, work_dir=work_dir,
            )
            if wait_ms:
                time.sleep(wait_ms / 1000.0)
            return shot
        except Exception as error:  # noqa: BLE001 - retry transient capture misses
            last_error = error
            print(f"[visual-pairs]   {stem}: attempt {attempt + 1} failed: {error}", flush=True)
    assert last_error is not None
    raise last_error


def generate_for_task(
    task_id: str,
    hidden_spec_path: Path,
    *,
    capture: str,
    repo: Path,
    headless: bool,
    wait_ms: int,
    retries: int,
    update_single_golden_from_after: bool = False,
) -> dict[str, Any]:
    raw_spec = yaml.safe_load(hidden_spec_path.read_text(encoding="utf-8"))
    if not isinstance(raw_spec, dict):
        raise ValueError(f"{task_id}: hidden spec is not a mapping: {hidden_spec_path}")
    spec = materialize_legacy_spec(raw_spec)
    spec.pop("evaluation", None)

    override = TASK_OVERRIDES.get(task_id, {})
    regen_before = override.get("regen_before", True)
    regen_after = override.get("regen_after", True)
    change_metrics: dict[str, float] | None = None

    before_spec = copy.deepcopy(spec)
    before_spec["actions"] = copy.deepcopy(override.get("before_actions", []))
    before_spec["assertions"] = []

    after_spec = copy.deepcopy(spec)
    after_prefix = override.get("after_prefix_actions")
    after_extra = override.get("after_extra_actions")
    if after_prefix or after_extra:
        after_spec["actions"] = [
            *copy.deepcopy(after_prefix or []),
            *spec.get("actions", []),
            *copy.deepcopy(after_extra or []),
        ]

    visual_dir = hidden_spec_path.parent / "visual"
    visual_dir.mkdir(parents=True, exist_ok=True)
    oracle_value = evaluation_visual_oracle(spec)
    oracle = oracle_value if isinstance(oracle_value, dict) else {}
    before_dest = _declared_golden_path(
        oracle.get("golden_before"), hidden_spec_path, visual_dir / f"{task_id}.before.golden.png"
    )
    after_dest = _declared_golden_path(
        oracle.get("golden_after"), hidden_spec_path, visual_dir / f"{task_id}.after.golden.png"
    )

    with tempfile.TemporaryDirectory(prefix=f"vp-{task_id}-") as tmp:
        work_dir = Path(tmp)
        before_shot: Path | None = None
        after_shot: Path | None = None
        if regen_before:
            before_shot = _capture(
                before_spec, capture=capture, repo=repo, src_spec_path=hidden_spec_path,
                stem=f"{task_id}-before", headless=headless, wait_ms=wait_ms, work_dir=work_dir, retries=retries,
            )
        if regen_after:
            after_shot = _capture(
                after_spec, capture=capture, repo=repo, src_spec_path=hidden_spec_path,
                stem=f"{task_id}-after", headless=headless, wait_ms=wait_ms, work_dir=work_dir, retries=retries,
            )
        before_candidate = before_shot or before_dest
        after_candidate = after_shot or after_dest
        if capture == "libreoffice_state":
            _assert_libreoffice_pair_is_distinct(
                task_id, before_candidate, after_candidate
            )
        if override.get("require_visible_change"):
            _assert_pair_has_visible_change(
                task_id, before_candidate, after_candidate
            )
        changed_pixel_ratio = override.get("require_changed_pixel_ratio")
        if changed_pixel_ratio is not None:
            change_metrics = _assert_pair_has_changed_region(
                task_id,
                before_candidate,
                after_candidate,
                minimum_changed_pixel_ratio=float(changed_pixel_ratio),
            )
        if override.get("require_rendered_flight_scene"):
            _assert_spacerpg_flight_scene_is_rendered(task_id, after_candidate)
        if before_shot is not None:
            shutil.copyfile(before_shot, before_dest)
        if after_shot is not None:
            shutil.copyfile(after_shot, after_dest)
        if update_single_golden_from_after:
            golden_dest = _single_golden_path(spec, hidden_spec_path)
            if golden_dest is not None and after_dest.exists():
                if golden_dest.resolve() != after_dest.resolve():
                    shutil.copyfile(after_dest, golden_dest)

    metadata_path = visual_dir / f"{task_id}.golden-meta.json"
    metadata = {
        "schema_version": 1,
        "task_id": task_id,
        "benchmark": spec.get("benchmark"),
        "source": "deterministic_reference_replay",
        "spec": str(hidden_spec_path.relative_to(REPO_ROOT)),
        "spec_sha256": hashlib.sha256(hidden_spec_path.read_bytes()).hexdigest(),
        "before": str(before_dest.relative_to(REPO_ROOT)),
        "before_sha256": hashlib.sha256(before_dest.read_bytes()).hexdigest(),
        "after": str(after_dest.relative_to(REPO_ROOT)),
        "after_sha256": hashlib.sha256(after_dest.read_bytes()).hexdigest(),
        "actions": len(spec.get("actions", [])),
    }
    metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    bq = screenshot_quality(before_dest) if before_dest.exists() else {"informative": False}
    aq = screenshot_quality(after_dest) if after_dest.exists() else {"informative": False}
    if change_metrics is None and before_dest.exists() and after_dest.exists():
        change_metrics = _pair_change_metrics(before_dest, after_dest)
    return {
        "task_id": task_id,
        "before": str(before_dest.relative_to(REPO_ROOT)),
        "after": str(after_dest.relative_to(REPO_ROOT)),
        "before_informative": bq["informative"],
        "after_informative": aq["informative"],
        "metadata": str(metadata_path.relative_to(REPO_ROOT)),
        "change_metrics": change_metrics,
    }


def _format_change_metrics(metrics: dict[str, float] | None) -> str:
    if not metrics:
        return "change=unknown"
    return (
        f"changed_pixels={metrics['changed_pixel_ratio']:.5f} "
        f"mean_diff={metrics['mean_absolute_difference']:.3f}"
    )


def _declared_golden_path(value: Any, spec_path: Path, fallback: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        return fallback.resolve()
    path = Path(value)
    if not path.is_absolute():
        path = spec_path.parent / path
    return path.resolve()


def _single_golden_path(spec: dict[str, Any], spec_path: Path) -> Path | None:
    visual_oracle = evaluation_visual_oracle(spec)
    if not isinstance(visual_oracle, dict):
        return None
    golden = visual_oracle.get("golden_image")
    if not isinstance(golden, str) or not golden.strip():
        return None
    path = Path(golden)
    if not path.is_absolute():
        path = spec_path.parent / path
    return path.resolve()


def tasks_for_benchmarks(
    benchmarks: list[str],
    task_filter: list[str] | None,
    *,
    c1_tasks: bool = False,
) -> list[tuple[str, str, Path]]:
    requested = {canonical_benchmark_id(benchmark) for benchmark in benchmarks}
    if c1_tasks:
        payload = yaml.safe_load(
            (REPO_ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
        )
        selected: list[tuple[str, str, Path]] = []
        for task in payload.get("tasks", []):
            task_id = str(task.get("id", ""))
            benchmark = canonical_benchmark_id(task.get("benchmark"))
            if benchmark not in requested or (
                task_filter and task_id not in task_filter
            ):
                continue
            selected.append(
                (task_id, benchmark, (REPO_ROOT / str(task["oracle_spec"])).resolve())
            )
        return selected
    oracles = load_hidden_oracles(REPO_ROOT)
    selected: list[tuple[str, str, Path]] = []
    for task_id, oracle in oracles.items():
        spec_path = resolve_hidden_spec(REPO_ROOT, oracle)
        if not spec_path.exists():
            continue
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        bench = canonical_benchmark_id((spec or {}).get("benchmark"))
        if bench not in requested:
            continue
        if task_filter and task_id not in task_filter:
            continue
        selected.append((task_id, bench, spec_path))
    return selected


def _png_dimensions(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"Not a valid PNG: {path}")
    return struct.unpack(">II", header[16:24])


def _review_rubric_html(title: str, rubric: Any, css_class: str) -> str:
    rubric = rubric if isinstance(rubric, dict) else {}
    groups = (
        ("Must", rubric.get("must")),
        ("Must not", rubric.get("must_not")),
        ("Ignore", rubric.get("ignore")),
    )
    rendered: list[str] = []
    for label, raw_items in groups:
        items = raw_items if isinstance(raw_items, list) else []
        if not items:
            continue
        rendered.append(
            f"<h4>{label}</h4><ul>"
            + "".join(
                f"<li>{html.escape(str(item))}</li>" for item in items
            )
            + "</ul>"
        )
    if not rendered:
        rendered.append('<p class="empty-rubric">No rubric constraints declared.</p>')
    return (
        f'<section class="rubric {css_class}">'
        f"<h3>{html.escape(title)}</h3>"
        + "".join(rendered)
        + "</section>"
    )


def refresh_existing_c1_golden_metadata() -> int:
    """Refresh only spec digests while proving registered PNG bytes did not drift."""

    payload = yaml.safe_load(
        (REPO_ROOT / "benchmarks/p5/c1_tasks.yaml").read_text(encoding="utf-8")
    )
    refreshed = 0
    for task in payload.get("tasks", []):
        task_id = str(task.get("id") or "")
        spec_path = (REPO_ROOT / str(task.get("oracle_spec") or "")).resolve()
        metadata_path = spec_path.parent / "visual" / f"{task_id}.golden-meta.json"
        if not metadata_path.is_file():
            continue
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("task_id") != task_id:
            raise ValueError(f"{metadata_path}: task_id does not match catalog")
        for role in ("before", "after"):
            relative = metadata.get(role)
            expected_sha = metadata.get(f"{role}_sha256")
            image_path = REPO_ROOT / str(relative or "")
            if not image_path.is_file():
                raise FileNotFoundError(image_path)
            actual_sha = hashlib.sha256(image_path.read_bytes()).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"{metadata_path}: {role} PNG digest changed; refusing metadata refresh"
                )
        metadata["spec_sha256"] = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        refreshed += 1
    if refreshed != 52:
        raise ValueError(f"Expected 52 existing C1 golden metadata files, found {refreshed}")
    return refreshed


def write_c1_review_artifacts() -> tuple[Path, Path]:
    manifest_path = REPO_ROOT / "benchmarks/p5/c1_tasks.yaml"
    payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    expansion_ids = set(
        ((payload.get("expansion_sets") or {}).get("deep30") or {}).get(
            "task_ids", []
        )
    )
    entries: list[dict[str, Any]] = []
    libreoffice_hashes: dict[str, str] = {}
    for task in payload.get("tasks", []):
        if task.get("id") not in expansion_ids:
            continue
        spec_path = (REPO_ROOT / str(task.get("oracle_spec", ""))).resolve()
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        oracle = evaluation_visual_oracle(spec) or {}
        before = _declared_golden_path(
            oracle.get("golden_before"), spec_path, spec_path.parent / "missing-before.png"
        )
        after = _declared_golden_path(
            oracle.get("golden_after"), spec_path, spec_path.parent / "missing-after.png"
        )
        for image_path in (before, after):
            if not image_path.is_file():
                raise FileNotFoundError(image_path)
            if not screenshot_quality(image_path)["informative"]:
                raise ValueError(f"Non-informative C1 golden: {image_path}")
        before_width, before_height = _png_dimensions(before)
        after_width, after_height = _png_dimensions(after)
        task_id = str(task["id"])
        before_sha = hashlib.sha256(before.read_bytes()).hexdigest()
        after_sha = hashlib.sha256(after.read_bytes()).hexdigest()
        if canonical_benchmark_id(task.get("benchmark")) == "libreoffice":
            if before_sha == after_sha:
                raise ValueError(
                    f"{task_id}: LibreOffice before/after goldens are identical"
                )
            for label, digest in (("before", before_sha), ("after", after_sha)):
                previous = libreoffice_hashes.get(digest)
                if previous is not None:
                    raise ValueError(
                        f"{task_id}:{label} duplicates LibreOffice golden "
                        f"{previous} ({digest})"
                    )
                libreoffice_hashes[digest] = f"{task_id}:{label}"
        metadata = {
            "schema_version": 1,
            "task_id": task_id,
            "benchmark": task.get("benchmark"),
            "source": "deterministic_reference_replay",
            "spec": str(spec_path.relative_to(REPO_ROOT)),
            "spec_sha256": hashlib.sha256(spec_path.read_bytes()).hexdigest(),
            "before": str(before.relative_to(REPO_ROOT)),
            "before_sha256": before_sha,
            "before_dimensions": [before_width, before_height],
            "after": str(after.relative_to(REPO_ROOT)),
            "after_sha256": after_sha,
            "after_dimensions": [after_width, after_height],
            "actions": len(spec.get("actions", [])),
        }
        metadata_path = before.parent / f"{task_id}.golden-meta.json"
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        entries.append(
            {
                **metadata,
                "preconditions": task.get("preconditions", ""),
                "reference_steps": task.get(
                    "reference_steps", ""
                ),
                "state_description": task.get("state_description", ""),
                "before_description": oracle.get("description", ""),
                "after_description": oracle.get("after_description", ""),
                "before_rubric": oracle.get("rubric", {}),
                "after_rubric": oracle.get("after_rubric", {}),
                "metadata": str(metadata_path.relative_to(REPO_ROOT)),
            }
        )
    if len(entries) != 30:
        raise ValueError(
            f"Expected exactly 30 expansion-task visual pairs, found {len(entries)}"
        )

    index_path = REPO_ROOT / "benchmarks/p5/c1_visual_goldens.json"
    index_payload = {
        "schema_version": 2,
        "source": "deterministic_reference_replay",
        "pair_count": len(entries),
        "image_count": len(entries) * 2,
        "tasks": entries,
    }
    index_path.write_text(
        json.dumps(index_payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )

    review_path = REPO_ROOT / "benchmarks/p5/c1_visual_review.html"
    cards: list[str] = []
    for entry in entries:
        before_src = (
            "../../"
            + entry["before"]
            + "?sha256="
            + entry["before_sha256"]
        )
        after_src = (
            "../../"
            + entry["after"]
            + "?sha256="
            + entry["after_sha256"]
        )
        cards.append(
            '<article class="card">'
            f'<h2>{html.escape(entry["task_id"])}</h2>'
            f'<p class="bench">{html.escape(str(entry["benchmark"]))} · '
            f'{entry["actions"]} actions</p>'
            '<section class="task-descriptions">'
            '<div class="description-field preconditions">'
            '<h3>preconditions</h3>'
            f'<p>{html.escape(str(entry["preconditions"]))}</p></div>'
            '<div class="description-field reference-steps">'
            '<h3>reference_steps</h3>'
            f'<p>{html.escape(str(entry["reference_steps"]))}</p></div>'
            '<div class="description-field state-description">'
            '<h3>state_description</h3>'
            f'<p>{html.escape(str(entry["state_description"]))}</p></div>'
            '</section>'
            '<div class="pair">'
            '<figure><figcaption>Before</figcaption>'
            f'<img src="{html.escape(before_src)}" alt="Before {html.escape(entry["task_id"])}">'
            f'<p>{html.escape(str(entry["before_description"]))}</p>'
            + _review_rubric_html(
                "Before rubric", entry["before_rubric"], "before-rubric"
            )
            + "</figure>"
            '<figure><figcaption>After</figcaption>'
            f'<img src="{html.escape(after_src)}" alt="After {html.escape(entry["task_id"])}">'
            f'<p>{html.escape(str(entry["after_description"]))}</p>'
            + _review_rubric_html(
                "After rubric", entry["after_rubric"], "after-rubric"
            )
            + "</figure>"
            '</div>'
            f'<details><summary>SHA-256</summary><code>before {entry["before_sha256"]}</code>'
            f'<code>after&nbsp; {entry["after_sha256"]}</code></details>'
            '</article>'
        )
    review_path.write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>C1 30-task visual golden review</title><style>'
        ':root{color-scheme:light;background:#f3f5f7;color:#17202a;'
        'font:15px/1.45 system-ui,sans-serif}body{max-width:1500px;margin:auto;padding:32px}'
        'h1{margin:0 0 8px}.summary{margin:0 0 28px;color:#506070}'
        '.card{background:white;border:1px solid #dce2e8;border-radius:14px;'
        'padding:20px;margin:0 0 24px;box-shadow:0 2px 10px #10203012}'
        'h2{margin:0}.bench{color:#5f6f7f}.pair{display:grid;grid-template-columns:1fr 1fr;gap:18px}'
        '.task-descriptions{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin:14px 0 20px}'
        '.description-field{border:1px solid #dce2e8;border-radius:9px;padding:10px 12px;background:#fbfcfd}'
        '.description-field h3{font:600 12px/1.3 ui-monospace,SFMono-Regular,monospace;'
        'color:#334155;margin:0 0 5px}.description-field p{margin:0;color:#263442}'
        '.preconditions{border-left:4px solid #64748b}'
        '.reference-steps{border-left:4px solid #2563eb}'
        '.state-description{border-left:4px solid #9333ea}'
        'figure{margin:0}figcaption{font-weight:700;margin-bottom:8px}'
        'img{display:block;width:100%;height:420px;object-fit:contain;background:#111;border-radius:8px}'
        'figure>p{min-height:42px}.rubric{border:1px solid #dce2e8;border-radius:10px;'
        'padding:12px 14px;margin-top:12px;background:#f8fafc}.rubric h3{font-size:15px;'
        'margin:0 0 8px}.rubric h4{font-size:13px;margin:10px 0 4px;color:#425466}'
        '.rubric ul{margin:0;padding-left:20px}.rubric li{margin:3px 0}'
        '.before-rubric{border-left:4px solid #64748b}.after-rubric{border-left:4px solid #16803c}'
        '.empty-rubric{min-height:0;color:#6b7280;margin:0}details code{display:block;overflow-wrap:anywhere}'
        '@media(max-width:900px){.pair,.task-descriptions{grid-template-columns:1fr}img{height:auto}}'
        '</style></head><body><h1>C1 expansion visual review</h1>'
        '<p class="summary">30 deterministic reference-replay pairs · 60 PNG images · '
        'before and after reviewed side by side.</p>'
        + "".join(cards)
        + '</body></html>\n',
        encoding="utf-8",
    )
    refresh_existing_c1_golden_metadata()
    return index_path, review_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--benchmark", nargs="+", help="Benchmarks to generate (e.g. 2048 DatingSim).")
    parser.add_argument("--task", action="append", dest="tasks", help="Limit to specific task ids. Repeatable.")
    parser.add_argument("--headed", action="store_true", help="Show the runtime window while capturing.")
    parser.add_argument("--wait-ms", type=int, default=400, help="Settle wait after each capture.")
    parser.add_argument("--retries", type=int, default=2, help="Capture attempts per frame (engine runtimes can miss frames).")
    parser.add_argument("--list", action="store_true", help="List benchmarks and exit.")
    parser.add_argument(
        "--c1",
        action="store_true",
        help="Read selected tasks from benchmarks/p5/c1_tasks.yaml instead of core hidden oracles.",
    )
    parser.add_argument(
        "--review-page",
        action="store_true",
        help="Refresh the 30-task C1 golden SHA index and side-by-side review page.",
    )
    args = parser.parse_args()

    if args.review_page:
        index_path, review_path = write_c1_review_artifacts()
        print(
            "[visual-pairs] wrote "
            f"{index_path.relative_to(REPO_ROOT)} and {review_path.relative_to(REPO_ROOT)}"
        )
        return 0

    if args.list or not args.benchmark:
        print("Available benchmarks (capture mode):")
        for name, cfg in BENCHMARKS.items():
            print(f"  {name:14} capture={cfg['capture']:7} repo={cfg['repo']}")
        return 0

    try:
        requested_benchmarks = [
            canonical_benchmark_id(benchmark) for benchmark in args.benchmark
        ]
    except ValueError as error:
        parser.error(str(error))

    selected = tasks_for_benchmarks(
        requested_benchmarks,
        args.tasks,
        c1_tasks=args.c1,
    )
    if not selected:
        print("[visual-pairs] no matching tasks.")
        return 1

    results: list[dict[str, Any]] = []
    failures: list[str] = []
    for task_id, bench, spec_path in selected:
        cfg = BENCHMARKS[bench]
        repo = (REPO_ROOT / cfg["repo"]).resolve()
        print(f"[visual-pairs] {bench}/{task_id} (capture={cfg['capture']}) <- {spec_path.relative_to(REPO_ROOT)} ...", flush=True)
        try:
            result = generate_for_task(
                task_id, spec_path, capture=cfg["capture"], repo=repo,
                headless=not args.headed, wait_ms=args.wait_ms, retries=args.retries,
                update_single_golden_from_after=bool(cfg.get("update_single_golden_from_after")),
            )
        except Exception as error:  # noqa: BLE001 - report and continue per task
            failures.append(f"{task_id}: {error}")
            print(f"[visual-pairs] FAILED {task_id}: {error}", flush=True)
            continue
        results.append(result)
        fb = "ok" if result["before_informative"] else "NON-INFORMATIVE"
        fa = "ok" if result["after_informative"] else "NON-INFORMATIVE"
        print(
            f"[visual-pairs] done {task_id}: before={fb} after={fa} "
            f"{_format_change_metrics(result['change_metrics'])}",
            flush=True,
        )

    print("\n[visual-pairs] summary:")
    for result in results:
        print(
            f"  {result['task_id']}: before={result['before']} ({'ok' if result['before_informative'] else 'NON-INFORMATIVE'}), "
            f"after={result['after']} ({'ok' if result['after_informative'] else 'NON-INFORMATIVE'}), "
            f"{_format_change_metrics(result['change_metrics'])}"
        )
    if failures:
        print("\n[visual-pairs] failures:")
        for failure in failures:
            print(f"  {failure}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
