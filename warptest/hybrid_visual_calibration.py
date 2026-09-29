"""Paper-grade hybrid visual-oracle calibration protocol.

This module deliberately keeps the historical :mod:`warptest.visual_experiment`
workflow intact.  ``hybrid-visual-calibration-v1`` is a frozen, staged protocol:

* ``prepare`` runs reference checkpoints, freezes real captures and constructs a
  balanced ten-profile calibration set;
* ``judge`` applies the exact C1 pair prompt with the registered visual model;
* two independent ``annotate`` sessions and one blinded ``adjudicate`` session
  produce reference labels, either from humans or explicitly identified VLMs;
  and
* ``aggregate`` reports judge calibration and the programmatic-vs-visual causal
  contrast for controlled pure-visual faults.

Provider calls happen in ``judge`` and, when explicitly selected, VLM-backed
``annotate``.  ``prepare`` installs the fake visual judge while reference
executions run, so capture qualification itself is zero-provider-call work.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import random
import statistics
import subprocess
import sys
import webbrowser
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import parse_qs, unquote, urlparse

import yaml
from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageStat

from . import cost
from .c1_evaluation import semantic_assertions, visual_oracle
from .c1_experiment import relay_environment
from .c1_desktop_runtime import LibreOfficeC1DesktopSession
from .c1_openra_runtime import (
    OpenRASettingsSession,
    apply_setting_actions as apply_openra_setting_actions,
    target_settings as openra_target_settings,
)
from .c1_web_runtime import JupyterLabC1WebSession
from .model_request import (
    model_request_profile_provenance,
    model_request_profiles_environment,
)
from .reports import new_report_dir, write_json
from .runner import run_spec
from .runtime import find_free_port
from .visual import (
    VISUAL_LABELS,
    VisualJudgeClient,
    _normalized_image_equality,
    build_pair_judge_prompt,
    screenshot_quality,
)


LEGACY_PROTOCOL = "hybrid-visual-calibration-v1"
PROTOCOL_V2 = "hybrid-visual-vlm-consensus-v2"
PROTOCOL = "hybrid-visual-vlm-consensus-v3"
SCHEMA_VERSION = 3
SELECTION_SALT = "visual-calibration-v3"
CALIBRATION_GOLDEN_POLICY = "same_execution_clean_frame_v1"
BOOTSTRAP_SEED = 20260830
BOOTSTRAP_ITERATIONS = 10_000
EXPECTED_BENCHMARKS = (
    "2048",
    "spacerpg",
    "datingsim",
    "jupyterlab",
    "luanti",
    "openra",
    "libreoffice",
    "blender",
    "jynew",
    "pal3",
)
BROWSER_OVERLAY_BENCHMARKS = frozenset(
    {"2048", "spacerpg", "datingsim", "jupyterlab"}
)
POST_RENDER_BENCHMARKS = frozenset(set(EXPECTED_BENCHMARKS) - BROWSER_OVERLAY_BENCHMARKS)
FORMAL_VARIANTS = (
    "before_clean",
    "after_clean",
    "same_profile_wrong_state",
    "blank_render",
    "critical_occlusion",
)
DESIGN_LABEL = {
    "before_clean": "correct",
    "after_clean": "correct",
    "same_profile_wrong_state": "incorrect",
    "blank_render": "incorrect",
    "critical_occlusion": "incorrect",
}
CALIBRATION_VISUAL_HEALTH_MUST = (
    "The application surface is coherently rendered and its task-relevant content remains readable.",
)
CALIBRATION_VISUAL_HEALTH_MUST_NOT = (
    "A large opaque overlay, distributed banding, blanked content, or comparable rendering corruption obscures a substantial part of the application surface.",
)
FAULT_VARIANTS = frozenset({"blank_render", "critical_occlusion"})
ANNOTATOR_IDS = ("annotator-a", "annotator-b")
ADJUDICATOR_ID = "adjudicator"
VLM_ANNOTATION_SCHEMA = "hybrid-visual-vlm-annotation-v1"
DEFAULT_VLM_ANNOTATOR_MODELS = {
    "annotator-a": "glm-5.3-flash@relay",
    "annotator-b": "doubao-seed-2-1-turbo@relay",
}
WRONG_STATE_DISTANCE_MIN = 0.01
BLANK_CHANGED_MIN = 0.50
OCCLUSION_CHANGED_MIN = 0.40
OCCLUSION_CHANGED_MAX = 0.70
SPACERPG_ANCHOR = "spacerpg-playable-flight"
PILOT_FALLBACK_REVISION = "hybrid-visual-pilot-fallback-v3-live-native-retry"
PILOT_TASK_ANCHORS = {
    "libreoffice": "calc-named-range-forecast",
}
PILOT_V1_DEVELOPMENT_TASK_IDS = frozenset(
    {
        "pilot-2048-near-loss",
        "pilot-spacerpg-station-manual-3-save",
        "pilot-datingsim-standing-date-low-energy",
        "pilot-jupyterlab-session-smoke",
        "pilot-luanti-mine-bridge-outpost",
        "pilot-openra-ra-allies07-soviet-war-factory-produces-heavy-tank",
        "pilot-libreoffice-calc-named-range-forecast",
        "pilot-blender-animation-keyframes",
        "pilot-jynew-item-use-medicine",
        "pilot-pal3-money-add",
    }
)
PILOT_VALIDATION_TASK_ANCHORS = {
    "libreoffice": "writer-board-report-toc",
}
PILOT_V2_VALIDATION_TASK_IDS = frozenset(
    {
        "pilot-2048-corner-merge-up",
        "pilot-spacerpg-equipment-install-cargo-expansion-repeat",
        "pilot-datingsim-ending-precondition",
        "pilot-jupyterlab-workspace-layout",
        "pilot-luanti-chest-inventory",
        "pilot-openra-cnc-gdi01-beachhead",
        "pilot-libreoffice-writer-board-report-toc",
        "pilot-blender-save-reopen-scene",
        "pilot-jynew-battle-setup",
        "pilot-pal3-money-restore",
    }
)
PILOT_CONFIRMATION_TASK_ANCHORS = {
    "libreoffice": "impress-slide-reorder-summary",
}
PILOT_CONFIRMATION_REUSE_TASK_IDS = {
    "blender": "pilot-blender-save-reopen-scene",
}


@dataclass(frozen=True)
class HybridTask:
    task_id: str
    benchmark: str
    oracle_spec: str
    horizon_class: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(value.encode("utf-8"))


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _source_provenance(repo_root: Path) -> dict[str, Any]:
    repo_root = repo_root.resolve()

    def git(*args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout

    status = git("status", "--porcelain=v2", "-uall")
    diff = git("diff", "--binary", "HEAD")
    source_paths = (
        "warptest/hybrid_visual_calibration.py",
        "warptest/visual.py",
        "warptest/cli.py",
        "warptest/model_request.py",
        "warptest/providers.py",
        "warptest/c1_desktop_runtime.py",
        "warptest/jupyterlab_runtime.py",
        "warptest/runtime.py",
    )
    return {
        "git_head": git("rev-parse", "HEAD").strip(),
        "dirty": bool(status.strip()),
        "git_status_sha256": _sha256_text(status),
        "git_diff_sha256": _sha256_text(diff),
        "source_files": {
            relative: _sha256_file(repo_root / relative)
            for relative in source_paths
        },
    }


def _json_hash(value: Any) -> str:
    return _sha256_text(_canonical_json(value))


def _benchmark_token(value: Any) -> str:
    token = (
        str(value or "")
        .strip()
        .lower()
        .replace("-", "")
        .replace("_", "")
        .replace(".", "")
        .replace(" ", "")
    )
    aliases = {
        "spacerpg": "spacerpg",
        "love": "datingsim",
        "libreoffice": "libreoffice",
        "pal3unity": "pal3",
        "minetest": "luanti",
    }
    return aliases.get(token, token)


def _load_mapping(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a mapping: {path}")
    return payload


def _resolve_from(path: Path, raw: str | Path) -> Path:
    value = Path(raw).expanduser()
    if value.is_absolute():
        return value.resolve()
    project_relative = (Path.cwd() / value).resolve()
    if project_relative.exists():
        return project_relative
    return (path.parent / value).resolve()


def _copy_image(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        ImageOps.exif_transpose(image).convert("RGBA").save(target, format="PNG")


def load_c1_tasks(task_file: Path) -> list[HybridTask]:
    payload = _load_mapping(task_file)
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError(f"C1 task file has no tasks list: {task_file}")
    tasks: list[HybridTask] = []
    seen: set[str] = set()
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            raise ValueError("Every C1 task must be a mapping")
        task_id = str(raw.get("id") or "").strip()
        benchmark = _benchmark_token(raw.get("benchmark"))
        oracle_spec = str(raw.get("oracle_spec") or "").strip()
        if not task_id or not benchmark or not oracle_spec:
            raise ValueError(f"C1 task lacks id/benchmark/oracle_spec: {raw!r}")
        if task_id in seen:
            raise ValueError(f"Duplicate C1 task id: {task_id}")
        seen.add(task_id)
        tasks.append(
            HybridTask(
                task_id=task_id,
                benchmark=benchmark,
                oracle_spec=str(_resolve_from(task_file, oracle_spec)),
                horizon_class=str(raw.get("horizon_class") or "unknown"),
            )
        )
    return tasks


def validate_task_visual_contract(
    task: HybridTask, *, allow_single_image: bool = False
) -> dict[str, Any]:
    spec_path = Path(task.oracle_spec)
    spec = _load_mapping(spec_path)
    oracle = visual_oracle(spec)
    if not isinstance(oracle, dict):
        raise ValueError(f"Task {task.task_id} has no visual oracle")
    paths: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for key in ("golden_before", "golden_after"):
        raw = oracle.get(key)
        if allow_single_image and (not isinstance(raw, str) or not raw.strip()):
            raw = oracle.get("golden_image")
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"Task {task.task_id} visual oracle lacks {key}")
        image_path = _resolve_from(spec_path, raw)
        if not image_path.is_file():
            raise ValueError(f"Task {task.task_id} {key} does not exist: {image_path}")
        quality = screenshot_quality(image_path)
        if not quality.get("informative"):
            raise ValueError(f"Task {task.task_id} {key} is non-informative")
        paths[key] = str(image_path)
        hashes[key] = _sha256_file(image_path)
    default_rubric = _normalized_rubric(oracle.get("rubric"))
    return {
        "spec_path": str(spec_path.resolve()),
        "spec_sha256": _sha256_file(spec_path),
        "golden_paths": paths,
        "golden_sha256": hashes,
        "before_description": str(oracle.get("before_description") or oracle.get("description") or ""),
        "after_description": str(oracle.get("after_description") or oracle.get("description") or ""),
        "before_rubric": _normalized_rubric(oracle.get("before_rubric"), fallback=default_rubric),
        "after_rubric": _normalized_rubric(oracle.get("after_rubric"), fallback=default_rubric),
    }


def _normalized_rubric(
    value: Any,
    *,
    fallback: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, list[str]]:
    source = value if isinstance(value, Mapping) else fallback or {}
    result: dict[str, list[str]] = {}
    for key in ("must", "must_not", "ignore"):
        raw = source.get(key, []) if isinstance(source, Mapping) else []
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            raise ValueError(f"visual rubric {key} must be a list")
        values = [str(item).strip() for item in raw if str(item).strip()]
        result[key] = values
    return result


def stable_task_order(task: HybridTask) -> str:
    return _sha256_text(f"{SELECTION_SALT}|{task.benchmark}|{task.task_id}")


def select_formal_tasks(
    eligible_tasks: Sequence[HybridTask],
    *,
    per_benchmark: int = 4,
) -> list[HybridTask]:
    grouped: dict[str, list[HybridTask]] = {name: [] for name in EXPECTED_BENCHMARKS}
    for task in eligible_tasks:
        if task.benchmark in grouped:
            grouped[task.benchmark].append(task)
    selected: list[HybridTask] = []
    for benchmark in EXPECTED_BENCHMARKS:
        choices = sorted(grouped[benchmark], key=lambda item: (stable_task_order(item), item.task_id))
        if benchmark == "spacerpg":
            anchor = next((task for task in choices if task.task_id == SPACERPG_ANCHOR), None)
            if anchor is None:
                raise ValueError(f"Formal SpaceRPG anchor is not eligible: {SPACERPG_ANCHOR}")
            choices = [anchor, *[task for task in choices if task.task_id != SPACERPG_ANCHOR]]
        if len(choices) < per_benchmark:
            raise ValueError(
                f"Formal hybrid calibration requires {per_benchmark} eligible tasks for "
                f"{benchmark}, found {len(choices)}"
            )
        selected.extend(choices[:per_benchmark])
    return selected


def select_pilot_tasks(
    task_file: Path,
    *,
    specs_root: Path = Path("specs"),
    cohort: str = "development",
) -> list[HybridTask]:
    if cohort not in {"development", "validation", "confirmation"}:
        raise ValueError(
            "pilot cohort must be development, validation, or confirmation"
        )
    formal_specs = {Path(task.oracle_spec).resolve() for task in load_c1_tasks(task_file)}
    candidates: dict[str, list[HybridTask]] = {name: [] for name in EXPECTED_BENCHMARKS}
    for spec_path in sorted(specs_root.glob("*/*.yaml")):
        resolved = spec_path.resolve()
        if resolved in formal_specs:
            continue
        try:
            spec = _load_mapping(resolved)
            oracle = visual_oracle(spec)
        except Exception:
            continue
        benchmark = _benchmark_token(spec.get("benchmark"))
        if benchmark not in candidates or not isinstance(oracle, dict):
            continue
        task = HybridTask(
            task_id=f"pilot-{benchmark}-{resolved.stem}",
            benchmark=benchmark,
            oracle_spec=str(resolved),
            horizon_class="pilot",
        )
        try:
            validate_task_visual_contract(task, allow_single_image=True)
        except Exception:
            continue
        candidates[benchmark].append(task)
    selected: list[HybridTask] = []
    for benchmark in EXPECTED_BENCHMARKS:
        choices = sorted(candidates[benchmark], key=lambda item: (stable_task_order(item), item.task_id))
        if not choices:
            raise ValueError(f"No non-formal pilot visual spec is available for {benchmark}")
        anchor_stem = PILOT_TASK_ANCHORS.get(benchmark)
        anchor = (
            next(
                (
                    task
                    for task in choices
                    if Path(task.oracle_spec).stem == anchor_stem
                ),
                None,
            )
            if anchor_stem
            else None
        )
        if anchor_stem and anchor is None:
            raise ValueError(
                f"Registered pilot anchor {anchor_stem!r} is unavailable for {benchmark}"
            )
        if cohort == "development":
            selected.append(anchor or choices[0])
        else:
            excluded_ids = set(PILOT_V1_DEVELOPMENT_TASK_IDS)
            if cohort == "confirmation":
                excluded_ids.update(PILOT_V2_VALIDATION_TASK_IDS)
            held_out = [
                task
                for task in choices
                if task.task_id not in excluded_ids
            ]
            if not held_out:
                reuse_task_id = PILOT_CONFIRMATION_REUSE_TASK_IDS.get(benchmark)
                reused = next(
                    (task for task in choices if task.task_id == reuse_task_id),
                    None,
                )
                if cohort != "confirmation" or reused is None:
                    raise ValueError(
                        f"No held-out {cohort} pilot visual spec is available for {benchmark}"
                    )
                held_out = [reused]
            cohort_anchors = (
                PILOT_CONFIRMATION_TASK_ANCHORS
                if cohort == "confirmation"
                else PILOT_VALIDATION_TASK_ANCHORS
            )
            validation_anchor = cohort_anchors.get(benchmark)
            anchored = (
                next(
                    (
                        task
                        for task in held_out
                        if Path(task.oracle_spec).stem == validation_anchor
                    ),
                    None,
                )
                if validation_anchor
                else None
            )
            if validation_anchor and anchored is None:
                raise ValueError(
                    f"Registered validation anchor {validation_anchor!r} is unavailable for {benchmark}"
                )
            selected.append(anchored or held_out[0])
    return selected


@contextlib.contextmanager
def _fake_visual_judge() -> Iterable[None]:
    previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
    os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
        else:
            os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous


def _phase_receipt(report: Any) -> dict[str, Any]:
    def phase(value: Any) -> dict[str, Any]:
        return {
            "status": getattr(value, "status", None),
            "detail": getattr(value, "detail", None),
            "data": getattr(value, "data", None),
        }

    payload = {
        "overall_status": report.overall_status,
        "validation": phase(report.validation),
        "smoke": phase(report.smoke_execution),
        "oracle": phase(report.oracle_result),
        "memory_persistence_diff": report.memory_persistence_diff,
        "soft_lock_signal": report.soft_lock_signal,
        "render_readiness_signal": report.render_readiness_signal,
        "console_errors": list(report.console_errors),
    }
    payload["projection_sha256"] = _json_hash(payload)
    payload["programmatic_pass"] = all(
        payload[name]["status"] == "success" for name in ("validation", "smoke", "oracle")
    )
    return payload


def _programmatic_failure_detail(payload: Mapping[str, Any]) -> str:
    details: list[str] = []
    for name in ("validation", "smoke", "oracle"):
        phase = payload.get(name)
        if not isinstance(phase, Mapping) or phase.get("status") == "success":
            continue
        detail = str(phase.get("detail") or phase.get("status") or "failure")
        data = phase.get("data")
        utility = data.get("utility_report") if isinstance(data, Mapping) else None
        utility_detail = utility.get("detail") if isinstance(utility, Mapping) else None
        if utility_detail and str(utility_detail) not in detail:
            detail = f"{detail}: {utility_detail}"
        details.append(f"{name}={detail}")
    return "; ".join(details) or "programmatic phases did not all pass"


def _browser_state_projection(benchmark: str, runtime: Any) -> dict[str, Any]:
    page = getattr(runtime, "page", None)
    if page is None:
        raise ValueError(f"{benchmark} runtime has no browser page for runtime overlay")
    if benchmark == "2048":
        value = page.evaluate(
            "() => window.__WARPTEST_2048__?.getState?.() ?? "
            "JSON.parse(localStorage.getItem('gameState') || 'null')"
        )
    elif benchmark == "spacerpg":
        value = page.evaluate(
            """() => {
              const s = window.__SPACERPG_E2E__?.getState?.() ?? {};
              return {
                screen: s.screen ?? null,
                currentSystemId: s.currentSystemId ?? null,
                currentStationId: s.currentStationId ?? null,
                credits: s.player?.credits ?? null,
                cargo: s.player?.cargo ?? null,
                throttle: s.flight?.throttle ?? s.throttle ?? null,
                activeSaveSlotId: s.activeSaveSlotId ?? null
              };
            }"""
        )
    elif benchmark == "datingsim":
        value = page.evaluate(
            """() => {
              const s = window.__WARPTEST_LOVE__?.getState?.() ?? {};
              return { player: s.player ?? null, world: s.world ?? null };
            }"""
        )
    elif benchmark == "jupyterlab":
        root = Path(getattr(runtime, "root_dir", ""))
        files: list[dict[str, Any]] = []
        if root.is_dir():
            for path in sorted(item for item in root.rglob("*") if item.is_file()):
                files.append(
                    {
                        "path": str(path.relative_to(root)),
                        "sha256": _sha256_file(path),
                    }
                )
        workspace = getattr(runtime, "_active_workspace", None)
        workspace_paths = (
            sorted(runtime.workspace_paths(workspace)) if workspace else []
        )
        value = {
            "files": files,
            "workspace": workspace,
            "workspace_paths": workspace_paths,
            "session_count": len(getattr(runtime, "sessions", [])),
        }
    else:
        raise ValueError(f"Runtime overlay is not registered for {benchmark}")
    return {"value": value, "sha256": _json_hash(value)}


def _overlay_javascript(kind: str) -> tuple[dict[str, Any], str]:
    if kind == "blank_render":
        style = {
            "left": "4vw",
            "top": "4vh",
            "width": "92vw",
            "height": "92vh",
            "background": "rgb(8, 8, 8)",
        }
    elif kind == "critical_occlusion":
        style = {
            "left": "0",
            "top": "0",
            "width": "100vw",
            "height": "100vh",
            "background": (
                "repeating-linear-gradient(90deg, rgb(20, 20, 20) 0, "
                "rgb(20, 20, 20) 3.125vw, transparent 3.125vw, "
                "transparent 4.6875vw)"
            ),
        }
    else:
        raise ValueError(f"Unknown runtime visual fault: {kind}")
    script = """(cfg) => {
      document.getElementById('warptest-hybrid-visual-fault')?.remove();
      const node = document.createElement('div');
      node.id = 'warptest-hybrid-visual-fault';
      node.setAttribute('aria-hidden', 'true');
      Object.assign(node.style, {
        position: 'fixed', zIndex: '2147483647', pointerEvents: 'auto',
        margin: '0', padding: '0', border: '0', color: 'transparent',
        ...cfg
      });
      document.documentElement.appendChild(node);
      return node.getBoundingClientRect().toJSON();
    }"""
    return style, script


def browser_fault_capture_hook(
    output: dict[str, Any],
    *,
    benchmark: str,
    capture_dir: Path,
):
    """Return a runner hook that captures two real-window visual interventions."""

    def hook(_spec: dict[str, Any], _spec_path: Path, runtime: Any, report: Any) -> None:
        if benchmark not in BROWSER_OVERLAY_BENCHMARKS:
            return
        capture_dir.mkdir(parents=True, exist_ok=True)
        clean_runtime_path = Path(runtime.screenshot("hybrid-runtime-clean.png")).resolve()
        clean_target = capture_dir / "clean.png"
        _copy_image(clean_runtime_path, clean_target)
        before = _browser_state_projection(benchmark, runtime)
        faults: dict[str, Any] = {}
        try:
            for variant in ("blank_render", "critical_occlusion"):
                style, script = _overlay_javascript(variant)
                rect = runtime.page.evaluate(script, style)
                state_during = _browser_state_projection(benchmark, runtime)
                if state_during["sha256"] != before["sha256"]:
                    raise ValueError(
                        f"{benchmark} {variant} changed the registered programmatic projection"
                    )
                raw_target = capture_dir / "raw" / f"{variant}.png"
                runtime_path = Path(getattr(runtime, "report_dir")) / f"hybrid-{variant}.png"
                runtime.page.screenshot(
                    path=str(runtime_path),
                    full_page=False,
                    animations="disabled",
                    timeout=10_000,
                )
                _copy_image(runtime_path, raw_target)
                target = capture_dir / f"{variant}.png"
                _normalize_runtime_fault(
                    clean_target, raw_target, target, variant
                )
                runtime.page.evaluate(
                    "() => document.getElementById('warptest-hybrid-visual-fault')?.remove()"
                )
                state_after = _browser_state_projection(benchmark, runtime)
                if state_after["sha256"] != before["sha256"]:
                    raise ValueError(
                        f"{benchmark} {variant} cleanup changed the registered programmatic projection"
                    )
                faults[variant] = {
                    "image": str(target.resolve()),
                    "image_sha256": _sha256_file(target),
                    "raw_runtime_image": str(raw_target.resolve()),
                    "raw_runtime_image_sha256": _sha256_file(raw_target),
                    "overlay_rect": rect,
                    "projection_before_sha256": before["sha256"],
                    "projection_during_sha256": state_during["sha256"],
                    "projection_after_sha256": state_after["sha256"],
                    "projection_equal": True,
                    "intervention_level": "runtime_overlay",
                    "pixel_normalization": "overlay_region_from_runtime_on_clean_control_v1",
                }
        finally:
            runtime.page.evaluate(
                "() => document.getElementById('warptest-hybrid-visual-fault')?.remove()"
            )
        output.update(
            {
                "benchmark": benchmark,
                "projection": before,
                "clean_image": str(clean_target.resolve()),
                "clean_image_sha256": _sha256_file(clean_target),
                "programmatic_receipt": _phase_receipt(report),
                "faults": faults,
            }
        )

    return hook


def _visual_paths_from_report(report: Any) -> dict[str, Path]:
    visual = report.visual_oracle_result.data
    if not isinstance(visual, dict) or visual.get("capture_failure"):
        raise ValueError("Reference execution did not produce valid visual evidence")
    before = visual.get("live_before")
    after = visual.get("live_after") or visual.get("live_screenshot")
    if not before:
        before = after
    golden_before = visual.get("golden_before") or visual.get("golden_image")
    golden_after = visual.get("golden_after") or visual.get("golden_image")
    paths = {
        "live_before": Path(str(before or "")).expanduser(),
        "live_after": Path(str(after or "")).expanduser(),
        "golden_before": Path(str(golden_before or "")).expanduser(),
        "golden_after": Path(str(golden_after or "")).expanduser(),
    }
    for name, path in paths.items():
        if not path.is_file():
            raise ValueError(f"Reference visual evidence lacks {name}: {path}")
        if not screenshot_quality(path).get("informative"):
            raise ValueError(f"Reference visual evidence is non-informative: {name}")
    return {key: value.resolve() for key, value in paths.items()}


def _reference_run(
    task: HybridTask,
    *,
    run_dir: Path,
    headed: bool,
    capture_runtime_faults: bool,
) -> dict[str, Any]:
    if task.benchmark == "libreoffice":
        return _libreoffice_reference_run(task, run_dir=run_dir)
    if task.benchmark == "openra":
        spec = _load_mapping(Path(task.oracle_spec).resolve())
        target = spec.get("target")
        if isinstance(target, Mapping) and target.get("kind") == "openra_settings":
            return _openra_settings_reference_run(task, run_dir=run_dir)
    if task.benchmark == "jupyterlab":
        return _jupyterlab_reference_run(
            task,
            run_dir=run_dir,
            capture_runtime_faults=capture_runtime_faults,
        )
    fault_receipt: dict[str, Any] = {}
    hook = (
        browser_fault_capture_hook(
            fault_receipt,
            benchmark=task.benchmark,
            capture_dir=run_dir / "runtime-faults",
        )
        if capture_runtime_faults and task.benchmark in BROWSER_OVERLAY_BENCHMARKS
        else None
    )
    with _fake_visual_judge():
        report = run_spec(
            task.oracle_spec,
            report_dir=run_dir,
            headless=not headed,
            visual_required=True,
            visual_on_semantic_failure=True,
            experimental_hook=hook,
        )
    programmatic = _phase_receipt(report)
    if not programmatic["programmatic_pass"]:
        raise ValueError(
            f"Reference execution did not pass programmatic validation for {task.task_id}: "
            f"{_programmatic_failure_detail(programmatic)}"
        )
    paths = _visual_paths_from_report(report)
    return {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "run_report": str((run_dir / "report.json").resolve()),
        "run_report_sha256": _sha256_file(run_dir / "report.json"),
        "programmatic": programmatic,
        "visual_paths": {key: str(value) for key, value in paths.items()},
        "visual_sha256": {key: _sha256_file(value) for key, value in paths.items()},
        "runtime_faults": fault_receipt,
    }


def _openra_settings_reference_run(
    task: HybridTask,
    *,
    run_dir: Path,
) -> dict[str, Any]:
    spec_path = Path(task.oracle_spec).resolve()
    spec = _load_mapping(spec_path)
    repo_raw = spec.get("repo")
    if not isinstance(repo_raw, str) or not repo_raw.strip():
        raise ValueError(f"OpenRA settings spec has no repository: {spec_path}")
    repo = _resolve_from(spec_path, repo_raw)
    start_settings = openra_target_settings(spec)
    actions = spec.get("actions")
    actions = actions if isinstance(actions, list) else []
    goal_settings = apply_openra_setting_actions(start_settings, actions)
    assertions = semantic_assertions(spec)
    assertions = assertions if isinstance(assertions, list) else []
    mod = str((spec.get("target") or {}).get("mod") or "ra")
    run_dir.mkdir(parents=True, exist_ok=True)

    with OpenRASettingsSession(
        repo,
        run_dir / "before-session",
        settings=start_settings,
        mod=mod,
        experiment_seed=0,
    ) as before_runtime:
        before_grade = before_runtime.grade_settings(start_settings)
        live_before = Path(
            before_runtime.screenshot(run_dir / "live-before.png")
        ).resolve()
        before_projection = before_runtime.seed_evidence(task.task_id)

    with OpenRASettingsSession(
        repo,
        run_dir / "after-session",
        settings=goal_settings,
        mod=mod,
        experiment_seed=0,
    ) as after_runtime:
        after_grade = (
            after_runtime.grade_assertions(assertions)
            if assertions
            else after_runtime.grade_settings(goal_settings)
        )
        live_after = Path(
            after_runtime.screenshot(run_dir / "live-after.png")
        ).resolve()
        after_projection = after_runtime.seed_evidence(task.task_id)

    programmatic_pass = bool(before_grade.ok and after_grade.ok)
    programmatic = {
        "overall_status": "success" if programmatic_pass else "failure",
        "validation": {
            "status": "success" if before_grade.ok else "failure",
            "detail": "OpenRA C1 public-settings start grade",
            "data": before_grade.detail,
        },
        "smoke": {
            "status": "success" if after_grade.ok else "failure",
            "detail": "OpenRA C1 public-settings registered transition",
            "data": {
                "actions": actions,
                "goal_settings": goal_settings,
            },
        },
        "oracle": {
            "status": "success" if after_grade.ok else "failure",
            "detail": "OpenRA C1 public-settings goal grade",
            "data": after_grade.detail,
        },
        "before_projection": before_projection,
        "after_projection": after_projection,
        "programmatic_pass": programmatic_pass,
    }
    programmatic["projection_sha256"] = _json_hash(programmatic)
    if not programmatic_pass:
        raise ValueError(
            f"OpenRA settings reference failed for {task.task_id}: "
            f"{_programmatic_failure_detail(programmatic)}"
        )

    contract = validate_task_visual_contract(task)
    paths = {
        "live_before": live_before,
        "live_after": live_after,
        "golden_before": Path(contract["golden_paths"]["golden_before"]),
        "golden_after": Path(contract["golden_paths"]["golden_after"]),
    }
    for name, path in paths.items():
        if not path.is_file() or not screenshot_quality(path).get("informative"):
            raise ValueError(f"OpenRA settings reference lacks informative {name}")
    report_path = run_dir / "report.json"
    write_json(
        report_path,
        {
            "schema": "hybrid-visual-openra-settings-reference-v1",
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "runtime": {
                "adapter": "OpenRASettingsSession",
                "capture_backend": OpenRASettingsSession.capture_backend,
                "input_backend": OpenRASettingsSession.input_backend,
            },
            "programmatic": programmatic,
        },
    )
    return {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "run_report": str(report_path.resolve()),
        "run_report_sha256": _sha256_file(report_path),
        "programmatic": programmatic,
        "visual_paths": {key: str(value.resolve()) for key, value in paths.items()},
        "visual_sha256": {key: _sha256_file(value) for key, value in paths.items()},
        "runtime_faults": {},
    }


def _jupyterlab_reference_run(
    task: HybridTask,
    *,
    run_dir: Path,
    capture_runtime_faults: bool,
) -> dict[str, Any]:
    spec_path = Path(task.oracle_spec).resolve()
    spec = _load_mapping(spec_path)
    repo_raw = spec.get("repo")
    if not isinstance(repo_raw, str) or not repo_raw.strip():
        raise ValueError(f"JupyterLab spec has no repository: {spec_path}")
    repo = _resolve_from(spec_path, repo_raw)
    run_dir.mkdir(parents=True, exist_ok=True)
    fault_receipt: dict[str, Any] = {}
    with JupyterLabC1WebSession(
        repo,
        run_dir / "session",
        headless=False,
        require_headed=True,
    ) as runtime:
        restored = runtime.restore_target_only(spec, spec_path)
        start = runtime.semantic_start(spec)
        live_before = Path(runtime.screenshot("hybrid-live-before.png")).resolve()
        transition = runtime.apply_registered_transition(spec)
        goal = runtime.semantic_goal(spec)
        live_after = Path(runtime.screenshot("hybrid-live-after.png")).resolve()
        programmatic_pass = bool(
            restored.status == "success"
            and restored.actions_executed is False
            and start.ok
            and transition.status == "success"
            and transition.actions_executed is True
            and goal.ok
        )
        programmatic = {
            "overall_status": "success" if programmatic_pass else "failure",
            "validation": {
                "status": "success" if start.ok else "failure",
                "detail": start.detail,
                "data": start.to_dict(),
            },
            "smoke": {
                "status": (
                    "success" if transition.status == "success" else "failure"
                ),
                "detail": transition.detail,
                "data": transition.to_dict(),
            },
            "oracle": {
                "status": "success" if goal.ok else "failure",
                "detail": goal.detail,
                "data": goal.to_dict(),
            },
            "restore": restored.to_dict(),
            "programmatic_pass": programmatic_pass,
        }
        programmatic["projection_sha256"] = _json_hash(programmatic)
        if capture_runtime_faults and programmatic_pass:
            adapter = runtime.runtime
            capture_dir = run_dir / "runtime-faults"
            capture_dir.mkdir(parents=True, exist_ok=True)
            clean_target = capture_dir / "clean.png"
            _copy_image(live_after, clean_target)
            before_projection = _browser_state_projection("jupyterlab", adapter)
            faults: dict[str, Any] = {}
            try:
                for variant in sorted(FAULT_VARIANTS):
                    style, script = _overlay_javascript(variant)
                    rect = runtime.page.evaluate(script, style)
                    during = _browser_state_projection("jupyterlab", adapter)
                    if during["sha256"] != before_projection["sha256"]:
                        raise ValueError(
                            f"jupyterlab {variant} changed the programmatic projection"
                        )
                    raw_target = capture_dir / "raw" / f"{variant}.png"
                    raw_target.parent.mkdir(parents=True, exist_ok=True)
                    runtime.page.screenshot(
                        path=str(raw_target),
                        full_page=False,
                        animations="disabled",
                        timeout=10_000,
                    )
                    target = capture_dir / f"{variant}.png"
                    _normalize_runtime_fault(
                        clean_target, raw_target, target, variant
                    )
                    runtime.page.evaluate(
                        "() => document.getElementById('warptest-hybrid-visual-fault')?.remove()"
                    )
                    after = _browser_state_projection("jupyterlab", adapter)
                    if after["sha256"] != before_projection["sha256"]:
                        raise ValueError(
                            f"jupyterlab {variant} cleanup changed the programmatic projection"
                        )
                    faults[variant] = {
                        "image": str(target.resolve()),
                        "image_sha256": _sha256_file(target),
                        "raw_runtime_image": str(raw_target.resolve()),
                        "raw_runtime_image_sha256": _sha256_file(raw_target),
                        "overlay_rect": rect,
                        "projection_before_sha256": before_projection["sha256"],
                        "projection_during_sha256": during["sha256"],
                        "projection_after_sha256": after["sha256"],
                        "projection_equal": True,
                        "intervention_level": "runtime_overlay",
                        "pixel_normalization": (
                            "overlay_region_from_runtime_on_clean_control_v1"
                        ),
                    }
            finally:
                runtime.page.evaluate(
                    "() => document.getElementById('warptest-hybrid-visual-fault')?.remove()"
                )
            fault_receipt.update(
                {
                    "benchmark": "jupyterlab",
                    "projection": before_projection,
                    "clean_image": str(clean_target.resolve()),
                    "clean_image_sha256": _sha256_file(clean_target),
                    "programmatic_receipt": programmatic,
                    "faults": faults,
                }
            )

    if not programmatic_pass:
        raise ValueError(
            f"JupyterLab C1 reference failed for {task.task_id}: "
            f"{_programmatic_failure_detail(programmatic)}"
        )
    contract = validate_task_visual_contract(task)
    paths = {
        "live_before": live_before,
        "live_after": live_after,
        "golden_before": Path(contract["golden_paths"]["golden_before"]),
        "golden_after": Path(contract["golden_paths"]["golden_after"]),
    }
    for name, path in paths.items():
        if not path.is_file() or not screenshot_quality(path).get("informative"):
            raise ValueError(f"JupyterLab C1 reference lacks informative {name}")
    report_path = run_dir / "report.json"
    write_json(
        report_path,
        {
            "schema": "hybrid-visual-jupyterlab-c1-reference-v1",
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "runtime": {
                "adapter": "JupyterLabC1WebSession",
                "capture_backend": runtime.capture_backend,
                "semantic_source": runtime.semantic_source,
            },
            "programmatic": programmatic,
        },
    )
    return {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "run_report": str(report_path.resolve()),
        "run_report_sha256": _sha256_file(report_path),
        "programmatic": programmatic,
        "visual_paths": {key: str(value.resolve()) for key, value in paths.items()},
        "visual_sha256": {key: _sha256_file(value) for key, value in paths.items()},
        "runtime_faults": fault_receipt,
    }


def _libreoffice_reference_run(
    task: HybridTask,
    *,
    run_dir: Path,
) -> dict[str, Any]:
    """Run LibreOffice through the Java UNO bridge used by formal C1.

    The legacy cold runtime depends on a Python/pyuno ABI match that is neither
    required nor used by C1.  This path exercises the registered Java bridge,
    native save/reopen contract and SVP/VCL capture backend instead.
    """

    spec_path = Path(task.oracle_spec).resolve()
    spec = _load_mapping(spec_path)
    repo_raw = spec.get("repo")
    if not isinstance(repo_raw, str) or not repo_raw.strip():
        raise ValueError(f"LibreOffice spec has no repository: {spec_path}")
    repo = _resolve_from(spec_path, repo_raw)
    run_dir.mkdir(parents=True, exist_ok=True)
    with LibreOfficeC1DesktopSession(repo, run_dir / "session") as runtime:
        restored = runtime.restore_target(spec)
        start = runtime.probe_start(spec)
        runtime.prepare_visual_capture(spec, "before")
        live_before = Path(runtime.screenshot(run_dir / "live-before.png")).resolve()
        goal = runtime.execute_state_transition(spec)
        runtime.prepare_visual_capture(spec, "after")
        live_after = Path(runtime.screenshot(run_dir / "live-after.png")).resolve()
        liveness = runtime.probe_liveness()
        programmatic_pass = bool(
            restored.get("status") == "success"
            and start.get("passed") is True
            and goal.get("passed") is True
            and liveness.get("passed") is True
        )
        programmatic = {
            "overall_status": "success" if programmatic_pass else "failure",
            "validation": {
                "status": "success" if start.get("passed") is True else "failure",
                "detail": "LibreOffice Java UNO start probe",
                "data": start,
            },
            "smoke": {
                "status": "success" if goal.get("passed") is True else "failure",
                "detail": "LibreOffice Java UNO state transition",
                "data": goal,
            },
            "oracle": {
                "status": "success" if goal.get("passed") is True else "failure",
                "detail": "LibreOffice Java UNO goal probe",
                "data": goal,
            },
            "liveness": liveness,
            "programmatic_pass": programmatic_pass,
        }
        programmatic["projection_sha256"] = _json_hash(programmatic)
        report_payload = {
            "schema": "hybrid-visual-libreoffice-reference-v1",
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "runtime": {
                "adapter": "LibreOfficeC1DesktopSession",
                "semantic_source": runtime.semantic_source,
                "capture_backend": runtime.capture_backend,
                "soffice": str(runtime.soffice),
                "bridge": "LibreOfficeC1Bridge.java",
            },
            "programmatic": programmatic,
            "logs": [str(path) for path in (runtime.stdout_path, runtime.stderr_path)],
        }
    if not programmatic_pass:
        raise ValueError(
            f"LibreOffice Java UNO reference failed for {task.task_id}: "
            f"{_programmatic_failure_detail(programmatic)}"
        )
    contract = validate_task_visual_contract(
        task, allow_single_image=task.horizon_class == "pilot"
    )
    report_path = run_dir / "report.json"
    write_json(report_path, report_payload)
    paths = {
        "live_before": live_before,
        "live_after": live_after,
        "golden_before": Path(contract["golden_paths"]["golden_before"]),
        "golden_after": Path(contract["golden_paths"]["golden_after"]),
    }
    for name, path in paths.items():
        if not path.is_file() or not screenshot_quality(path).get("informative"):
            raise ValueError(f"LibreOffice Java UNO reference lacks informative {name}")
    return {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "run_report": str(report_path.resolve()),
        "run_report_sha256": _sha256_file(report_path),
        "programmatic": programmatic,
        "visual_paths": {key: str(value.resolve()) for key, value in paths.items()},
        "visual_sha256": {key: _sha256_file(value) for key, value in paths.items()},
        "runtime_faults": {},
    }


def qualify_tasks(
    tasks: Sequence[HybridTask],
    *,
    output_dir: Path,
    headed: bool,
    allow_single_image: bool = False,
    allow_pilot_fallback: bool = True,
) -> tuple[list[HybridTask], list[dict[str, Any]]]:
    eligible: list[HybridTask] = []
    audit: list[dict[str, Any]] = []
    prior_rows: dict[str, dict[str, Any]] = {}
    audit_path = output_dir / "qualification-audit.json"
    if audit_path.is_file():
        try:
            prior_payload = _load_mapping(audit_path)
            prior_rows = {
                str(item.get("task_id")): item
                for item in prior_payload.get("tasks", [])
                if isinstance(item, dict) and item.get("task_id")
            }
        except Exception:
            prior_rows = {}
    for task in tasks:
        prior = prior_rows.get(task.task_id)
        if (
            prior
            and prior.get("eligible") is True
            and _pilot_qualification_reusable(prior)
            and (
                prior.get("eligibility_mode")
                != "registered_golden_pilot_fallback"
                or allow_pilot_fallback
            )
        ):
            if prior.get("eligibility_mode") == "registered_golden_pilot_fallback":
                prior = dict(prior)
                prior["fallback_revision"] = PILOT_FALLBACK_REVISION
                prior["fallback_reason"] = _pilot_prior_failure_detail(
                    output_dir,
                    task,
                    default=str(prior.get("fallback_reason") or prior.get("error") or ""),
                )
            audit.append(prior)
            eligible.append(task)
            continue
        if (
            allow_single_image
            and allow_pilot_fallback
            and prior
            and prior.get("eligible") is not True
            and prior.get("error")
        ):
            # Preserve the already-observed infrastructure failure instead of
            # reopening a native application merely to discover it again.
            validate_task_visual_contract(task, allow_single_image=True)
            fallback = dict(prior)
            fallback["eligible"] = True
            fallback["eligibility_mode"] = "registered_golden_pilot_fallback"
            fallback["fallback_revision"] = PILOT_FALLBACK_REVISION
            fallback["fallback_reason"] = _pilot_prior_failure_detail(
                output_dir, task, default=str(prior["error"])
            )
            audit.append(fallback)
            eligible.append(task)
            write_json(
                audit_path,
                {"schema": PROTOCOL, "schema_version": SCHEMA_VERSION, "tasks": audit},
            )
            continue
        row: dict[str, Any] = {
            "task_id": task.task_id,
            "benchmark": task.benchmark,
            "eligible": False,
        }
        try:
            row["visual_contract"] = validate_task_visual_contract(
                task, allow_single_image=allow_single_image
            )
            receipts = []
            for repeat in range(2):
                receipts.append(
                    _reference_run(
                        task,
                        run_dir=output_dir / "qualification" / task.task_id / f"repeat-{repeat}",
                        headed=headed,
                        capture_runtime_faults=False,
                    )
                )
            row["reference_receipts"] = receipts
            row["eligible"] = True
            row["eligibility_mode"] = "reference_execution"
            eligible.append(task)
        except Exception as error:
            row["error"] = f"{type(error).__name__}: {error}"
            if allow_single_image and allow_pilot_fallback:
                # Pilot is an engineering shakeout, not paper evidence.  A
                # registered-golden fallback keeps all ten annotation paths
                # testable when local native dependencies or an old Unity
                # license are unavailable.  Formal qualification never enters
                # this branch.
                row["eligible"] = True
                row["eligibility_mode"] = "registered_golden_pilot_fallback"
                row["fallback_revision"] = PILOT_FALLBACK_REVISION
                row["fallback_reason"] = row["error"]
                eligible.append(task)
        audit.append(row)
        write_json(
            audit_path,
            {"schema": PROTOCOL, "schema_version": SCHEMA_VERSION, "tasks": audit},
        )
    write_json(
        audit_path,
        {"schema": PROTOCOL, "schema_version": SCHEMA_VERSION, "tasks": audit},
    )
    return eligible, audit


def _pilot_qualification_reusable(row: Mapping[str, Any]) -> bool:
    mode = row.get("eligibility_mode")
    if mode == "registered_golden_pilot_fallback":
        return row.get("fallback_revision") == PILOT_FALLBACK_REVISION
    if mode not in {None, "reference_execution"}:
        return False
    receipts = row.get("reference_receipts")
    if not isinstance(receipts, list) or len(receipts) != 2:
        return False
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            return False
        path = Path(str(receipt.get("run_report") or ""))
        if not path.is_file() or receipt.get("run_report_sha256") != _sha256_file(path):
            return False
        programmatic = receipt.get("programmatic")
        if not isinstance(programmatic, Mapping) or programmatic.get("programmatic_pass") is not True:
            return False
    return True


def _pilot_prior_failure_detail(
    output_dir: Path,
    task: HybridTask,
    *,
    default: str,
) -> str:
    report_path = output_dir / "qualification" / task.task_id / "repeat-0" / "report.json"
    if not report_path.is_file():
        return default
    try:
        report = _load_mapping(report_path)
        phases = {
            name: report.get(source)
            for name, source in (
                ("validation", "validation"),
                ("smoke", "smoke_execution"),
                ("oracle", "oracle_result"),
            )
        }
        detail = _programmatic_failure_detail(phases)
        return f"{default}; {detail}" if detail else default
    except Exception:
        return default


def corrupt_image(source: Path, target: Path, variant: str) -> dict[str, Any]:
    with Image.open(source) as raw:
        base = ImageOps.exif_transpose(raw).convert("RGBA")
    result = base.copy()
    width, height = result.size
    draw = ImageDraw.Draw(result)
    if variant == "blank_render":
        left, top = round(width * 0.04), round(height * 0.04)
        right, bottom = round(width * 0.96), round(height * 0.96)
        fill = (8, 8, 8, 255)
    elif variant == "critical_occlusion":
        left, top, right, bottom = 0, 0, width, height
        fill = (20, 20, 20, 255)
    else:
        raise ValueError(f"Unsupported hybrid visual corruption: {variant}")
    if variant == "critical_occlusion":
        stripe = max(6, round(width / 64))
        period = stripe * 3
        for x in range(0, width, period):
            draw.rectangle(
                (x, 0, min(width, x + stripe * 2) - 1, height - 1),
                fill=fill,
            )
    else:
        draw.rectangle((left, top, right - 1, bottom - 1), fill=fill)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.save(target, format="PNG")
    return image_intervention_receipt(source, target, variant)


def _normalized_fault_box(width: int, height: int, variant: str) -> tuple[int, int, int, int]:
    if variant == "blank_render":
        return (
            round(width * 0.04),
            round(height * 0.04),
            round(width * 0.96),
            round(height * 0.96),
        )
    if variant == "critical_occlusion":
        return (0, 0, width, height)
    raise ValueError(f"Unsupported hybrid visual corruption: {variant}")


def _normalize_runtime_fault(
    clean: Path,
    raw_fault: Path,
    target: Path,
    variant: str,
) -> None:
    """Freeze dynamic pixels outside the actual runtime overlay treatment.

    WebGL and game cameras can animate between two adjacent screenshots.  The raw
    runtime frame is retained as evidence, while the formal calibration image
    pastes only the registered overlay rectangle onto the clean control.  This
    isolates the intended visual treatment without fabricating its pixels.
    """

    with Image.open(clean) as clean_raw, Image.open(raw_fault) as fault_raw:
        base = ImageOps.exif_transpose(clean_raw).convert("RGBA")
        fault = ImageOps.exif_transpose(fault_raw).convert("RGBA")
    if fault.size != base.size:
        raise ValueError(
            f"runtime fault changed image dimensions from {base.size} to {fault.size}"
        )
    result = base.copy()
    if variant == "critical_occlusion":
        width, height = base.size
        stripe = max(6, round(width / 64))
        period = stripe * 3
        for x in range(0, width, period):
            box = (x, 0, min(width, x + stripe * 2), height)
            result.paste(fault.crop(box), box)
    else:
        box = _normalized_fault_box(*base.size, variant)
        result.paste(fault.crop(box), box)
    target.parent.mkdir(parents=True, exist_ok=True)
    result.save(target, format="PNG")


def image_intervention_receipt(source: Path, target: Path, variant: str) -> dict[str, Any]:
    with Image.open(source) as raw_source, Image.open(target) as raw_target:
        base = ImageOps.exif_transpose(raw_source).convert("RGBA")
        result = ImageOps.exif_transpose(raw_target).convert("RGBA")
    if result.size != base.size:
        raise ValueError(f"{variant} changed image dimensions from {base.size} to {result.size}")
    width, height = base.size
    if variant == "blank_render":
        left, top, right, bottom = _normalized_fault_box(width, height, variant)
        lower, upper = BLANK_CHANGED_MIN, 1.0
    elif variant == "critical_occlusion":
        left, top, right, bottom = _normalized_fault_box(width, height, variant)
        lower, upper = OCCLUSION_CHANGED_MIN, OCCLUSION_CHANGED_MAX
    else:
        raise ValueError(f"Unsupported hybrid visual corruption: {variant}")
    difference = ImageChops.difference(base, result).convert("RGB")
    changed_mask = difference.convert("L").point(lambda value: 255 if value else 0)
    changed = width * height - changed_mask.histogram()[0]
    fraction = changed / (width * height)
    if fraction <= lower or fraction > upper:
        raise ValueError(
            f"{variant} changed-pixel fraction {fraction:.6f} outside ({lower}, {upper}]"
        )
    return {
        "variant": variant,
        "source_sha256": _sha256_file(source),
        "output_sha256": _sha256_file(target),
        "width": width,
        "height": height,
        "bounding_box": [left, top, right, bottom],
        "changed_pixel_fraction": fraction,
    }


def normalized_pixel_distance(first: Path, second: Path) -> float:
    with Image.open(first) as raw_first, Image.open(second) as raw_second:
        left = ImageOps.exif_transpose(raw_first).convert("RGB")
        right = ImageOps.exif_transpose(raw_second).convert("RGB")
    if right.size != left.size:
        right = right.resize(left.size, Image.Resampling.LANCZOS)
    stat = ImageStat.Stat(ImageChops.difference(left, right))
    return sum(stat.mean) / (3.0 * 255.0)


def choose_wrong_state_donor(
    base: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], float]:
    choices: list[tuple[float, str, Mapping[str, Any]]] = []
    source = Path(str(base["frozen_images"]["live_after"]))
    for candidate in candidates:
        if candidate["task_id"] == base["task_id"]:
            continue
        target = Path(str(candidate["frozen_images"]["live_after"]))
        distance = normalized_pixel_distance(source, target)
        choices.append((distance, str(candidate["task_id"]), candidate))
    pilot_donor = base.get("pilot_wrong_state_donor")
    if not choices and isinstance(pilot_donor, Mapping):
        target = Path(str(pilot_donor.get("image") or ""))
        if target.is_file():
            distance = normalized_pixel_distance(source, target)
            pseudo = {
                "task_id": str(pilot_donor.get("donor_id") or "pilot-golden-donor"),
                "frozen_images": {"live_after": str(target)},
            }
            choices.append((distance, str(pseudo["task_id"]), pseudo))
    if not choices:
        raise ValueError(f"No wrong-state donor is available for {base['task_id']}")
    distance, _, donor = max(choices, key=lambda item: (item[0], item[1]))
    if distance < WRONG_STATE_DISTANCE_MIN:
        raise ValueError(
            f"Wrong-state donor for {base['task_id']} is too similar: {distance:.6f}"
        )
    return donor, distance


def _pilot_wrong_state_donor(
    base: Mapping[str, Any],
    *,
    specs_root: Path = Path("specs"),
) -> dict[str, Any]:
    source = Path(str(base["frozen_images"]["live_after"]))
    current_spec = Path(str(base["oracle_spec"])).resolve()
    choices: list[tuple[float, str, Path]] = []
    for spec_path in sorted(specs_root.glob("*/*.yaml")):
        resolved = spec_path.resolve()
        if resolved == current_spec:
            continue
        try:
            spec = _load_mapping(resolved)
            if _benchmark_token(spec.get("benchmark")) != base["benchmark"]:
                continue
            oracle = visual_oracle(spec)
            if not isinstance(oracle, Mapping):
                continue
            raw = oracle.get("golden_after") or oracle.get("golden_image")
            if not isinstance(raw, str) or not raw.strip():
                continue
            image = _resolve_from(resolved, raw)
            if not image.is_file() or not screenshot_quality(image).get("informative"):
                continue
            distance = normalized_pixel_distance(source, image)
            choices.append((distance, resolved.stem, image))
        except Exception:
            continue
    if not choices:
        raise ValueError(f"No registered pilot wrong-state donor for {base['task_id']}")
    distance, donor_id, image = max(choices, key=lambda item: (item[0], item[1]))
    if distance < WRONG_STATE_DISTANCE_MIN:
        raise ValueError(
            f"Registered pilot donor for {base['task_id']} is too similar: {distance:.6f}"
        )
    return {
        "source": "pilot_registered_golden_donor",
        "donor_id": f"pilot-donor-{donor_id}",
        "image": str(image.resolve()),
        "image_sha256": _sha256_file(image),
        "distance": distance,
    }


def _freeze_base_capture(
    task: HybridTask,
    *,
    output_dir: Path,
    headed: bool,
) -> dict[str, Any]:
    base_dir = output_dir / "base-captures" / task.task_id
    receipt = _reference_run(
        task,
        run_dir=base_dir / "run",
        headed=headed,
        capture_runtime_faults=True,
    )
    contract = validate_task_visual_contract(
        task, allow_single_image=task.horizon_class == "pilot"
    )
    frozen_dir = base_dir / "frozen"
    frozen: dict[str, str] = {}
    for name, raw in receipt["visual_paths"].items():
        target = frozen_dir / f"{name}.png"
        _copy_image(Path(raw), target)
        frozen[name] = str(target.resolve())
    runtime_faults = receipt.get("runtime_faults") or {}
    fault_images: dict[str, str] = {}
    if task.benchmark in BROWSER_OVERLAY_BENCHMARKS:
        faults = runtime_faults.get("faults") if isinstance(runtime_faults, dict) else None
        if not isinstance(faults, dict) or set(faults) != set(FAULT_VARIANTS):
            raise ValueError(f"Runtime overlay capture is incomplete for {task.task_id}")
        projection = runtime_faults.get("projection")
        clean_image = runtime_faults.get("clean_image")
        if not isinstance(clean_image, str) or not Path(clean_image).is_file():
            raise ValueError(f"Runtime overlay clean capture is missing for {task.task_id}")
        _copy_image(Path(clean_image), Path(frozen["live_after"]))
        for variant in sorted(FAULT_VARIANTS):
            if not faults[variant].get("projection_equal"):
                raise ValueError(f"Runtime overlay projection changed for {task.task_id}/{variant}")
            target = frozen_dir / f"{variant}.png"
            _copy_image(Path(faults[variant]["image"]), target)
            fault_images[variant] = str(target.resolve())
        intervention_level = "runtime_overlay"
        state_projection = projection
    else:
        for variant in sorted(FAULT_VARIANTS):
            target = frozen_dir / f"{variant}.png"
            corrupt_image(Path(frozen["live_after"]), target, variant)
            fault_images[variant] = str(target.resolve())
        intervention_level = "post_render"
        state_projection = {
            "value": receipt["programmatic"],
            "sha256": receipt["programmatic"]["projection_sha256"],
        }
    corruption_receipts = {
        variant: image_intervention_receipt(
            Path(frozen["live_after"]), Path(fault_images[variant]), variant
        )
        for variant in sorted(FAULT_VARIANTS)
    }
    row = {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "horizon_class": task.horizon_class,
        "oracle_spec": task.oracle_spec,
        "contract": contract,
        "reference_receipt": receipt,
        "frozen_images": frozen,
        "fault_images": fault_images,
        "intervention_level": intervention_level,
        "state_projection": state_projection,
        "corruption_receipts": corruption_receipts,
    }
    write_json(base_dir / "base-receipt.json", row)
    return row


def _freeze_pilot_fallback_capture(
    task: HybridTask,
    *,
    output_dir: Path,
    fallback_reason: str,
) -> dict[str, Any]:
    """Build a non-paper pilot base from registered golden evidence only."""

    base_dir = output_dir / "base-captures" / task.task_id
    frozen_dir = base_dir / "frozen"
    contract = validate_task_visual_contract(task, allow_single_image=True)
    frozen: dict[str, str] = {}
    for stage in ("before", "after"):
        source = Path(contract["golden_paths"][f"golden_{stage}"])
        for role in ("golden", "live"):
            target = frozen_dir / f"{role}_{stage}.png"
            _copy_image(source, target)
            frozen[f"{role}_{stage}"] = str(target.resolve())
    fault_images: dict[str, str] = {}
    corruption_receipts: dict[str, Any] = {}
    for variant in sorted(FAULT_VARIANTS):
        target = frozen_dir / f"{variant}.png"
        corruption_receipts[variant] = corrupt_image(
            Path(frozen["live_after"]), target, variant
        )
        fault_images[variant] = str(target.resolve())
    fallback_projection = {
        "task_id": task.task_id,
        "contract_sha256": contract["spec_sha256"],
        "golden_sha256": contract["golden_sha256"],
        "fallback": True,
    }
    programmatic = {
        "programmatic_pass": False,
        "not_evaluated": True,
        "reason": fallback_reason,
        "projection_sha256": _json_hash(fallback_projection),
    }
    row = {
        "task_id": task.task_id,
        "benchmark": task.benchmark,
        "horizon_class": task.horizon_class,
        "oracle_spec": task.oracle_spec,
        "contract": contract,
        "reference_receipt": {
            "pilot_only": True,
            "paper_evidence": False,
            "capture_mode": "registered_golden_pilot_fallback",
            "fallback_reason": fallback_reason,
            "programmatic": programmatic,
        },
        "frozen_images": frozen,
        "fault_images": fault_images,
        "intervention_level": "post_render_pilot_fallback",
        "state_projection": {
            "value": fallback_projection,
            "sha256": programmatic["projection_sha256"],
        },
        "corruption_receipts": corruption_receipts,
    }
    write_json(base_dir / "base-receipt.json", row)
    return row


def _pilot_base_capture_reusable(task: HybridTask, base_dir: Path) -> dict[str, Any] | None:
    receipt_path = base_dir / "base-receipt.json"
    if not receipt_path.is_file():
        return None
    try:
        row = _load_mapping(receipt_path)
        if row.get("task_id") != task.task_id or row.get("benchmark") != task.benchmark:
            return None
        contract = row.get("contract")
        current = validate_task_visual_contract(task, allow_single_image=True)
        if not isinstance(contract, Mapping) or contract.get("spec_sha256") != current["spec_sha256"]:
            return None
        for path in [
            *(row.get("frozen_images") or {}).values(),
            *(row.get("fault_images") or {}).values(),
        ]:
            image = Path(str(path))
            if not image.is_file() or not screenshot_quality(image).get("informative"):
                return None
        for variant, corruption in (row.get("corruption_receipts") or {}).items():
            if variant not in FAULT_VARIANTS or not isinstance(corruption, Mapping):
                return None
            target = Path(str((row.get("fault_images") or {}).get(variant) or ""))
            if corruption.get("output_sha256") != _sha256_file(target):
                return None
        return row
    except Exception:
        return None


def build_samples_from_bases(
    bases: Sequence[Mapping[str, Any]],
    *,
    output_dir: Path,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for base in bases:
        grouped.setdefault(str(base["benchmark"]), []).append(base)
    internal: list[dict[str, Any]] = []
    for base in sorted(bases, key=lambda item: (str(item["benchmark"]), str(item["task_id"]))):
        donor, donor_distance = choose_wrong_state_donor(
            base, grouped[str(base["benchmark"])]
        )
        contract = base["contract"]
        for variant in FORMAL_VARIANTS:
            stage = "before" if variant == "before_clean" else "after"
            description = contract[f"{stage}_description"]
            rubric = {
                key: list(contract[f"{stage}_rubric"].get(key, []))
                for key in ("must", "must_not", "ignore")
            }
            for item in CALIBRATION_VISUAL_HEALTH_MUST:
                if item not in rubric["must"]:
                    rubric["must"].append(item)
            for item in CALIBRATION_VISUAL_HEALTH_MUST_NOT:
                if item not in rubric["must_not"]:
                    rubric["must_not"].append(item)
            production_golden_source = Path(
                base["frozen_images"][f"golden_{stage}"]
            )
            golden_source = Path(base["frozen_images"][f"live_{stage}"])
            if variant == "before_clean":
                live_source = Path(base["frozen_images"]["live_before"])
            elif variant == "after_clean":
                live_source = Path(base["frozen_images"]["live_after"])
            elif variant == "same_profile_wrong_state":
                live_source = Path(donor["frozen_images"]["live_after"])
            else:
                live_source = Path(base["fault_images"][variant])
            internal.append(
                {
                    "base_task_id": base["task_id"],
                    "benchmark": base["benchmark"],
                    "stage": stage,
                    "variant": variant,
                    "design_label": DESIGN_LABEL[variant],
                    "description": description,
                    "rubric": rubric,
                    "golden_source": str(golden_source),
                    "production_golden_source": str(production_golden_source),
                    "live_source": str(live_source),
                    "calibration_golden_policy": CALIBRATION_GOLDEN_POLICY,
                    "intervention_level": (
                        base["intervention_level"] if variant in FAULT_VARIANTS else "not_applicable"
                    ),
                    "programmatic_pass": bool(base["reference_receipt"]["programmatic"]["programmatic_pass"]),
                    "state_projection_sha256": base["state_projection"]["sha256"],
                    "wrong_state_donor_task_id": (
                        donor["task_id"] if variant == "same_profile_wrong_state" else None
                    ),
                    "wrong_state_distance": (
                        donor_distance if variant == "same_profile_wrong_state" else None
                    ),
                }
            )
    internal.sort(
        key=lambda row: _sha256_text(
            f"{PROTOCOL}|{row['benchmark']}|{row['base_task_id']}|{row['variant']}"
        )
    )
    samples: list[dict[str, Any]] = []
    for index, row in enumerate(internal, start=1):
        sample_id = f"s{index:03d}"
        sample_dir = output_dir / "samples" / sample_id
        golden = sample_dir / "golden.png"
        production_golden = sample_dir / "production-golden.png"
        live = sample_dir / "live.png"
        _copy_image(Path(row.pop("golden_source")), golden)
        _copy_image(Path(row.pop("production_golden_source")), production_golden)
        _copy_image(Path(row.pop("live_source")), live)
        receipt = {
            "schema": PROTOCOL,
            "schema_version": SCHEMA_VERSION,
            "sample_id": sample_id,
            **row,
            "golden": str(golden.resolve()),
            "production_golden": str(production_golden.resolve()),
            "live": str(live.resolve()),
            "golden_sha256": _sha256_file(golden),
            "production_golden_sha256": _sha256_file(production_golden),
            "live_sha256": _sha256_file(live),
        }
        receipt["receipt_sha256"] = _json_hash(receipt)
        write_json(sample_dir / "sample-receipt.json", receipt)
        samples.append(receipt)
    return samples


def prepare_hybrid_dataset(
    *,
    task_file: Path = Path("benchmarks/p5/c1_tasks.yaml"),
    output_dir: Path | None = None,
    split: str = "formal",
    headed: bool = True,
    pilot_cohort: str = "development",
) -> Path:
    if split not in {"pilot", "formal"}:
        raise ValueError("hybrid visual split must be pilot or formal")
    if not headed:
        raise ValueError(
            "hybrid-visual-calibration-v1 prepare requires headed execution "
            "for registered real capture backends"
        )
    root = (output_dir or (new_report_dir() / f"hybrid-visual-{split}")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    prior_created_at: str | None = None
    prior_manifest_path = root / "visual-calibration-manifest.json"
    if prior_manifest_path.is_file():
        try:
            prior_manifest = _load_mapping(prior_manifest_path)
            if prior_manifest.get("schema") == PROTOCOL and prior_manifest.get("split") == split:
                prior_created_at = str(prior_manifest.get("created_at_utc") or "") or None
        except Exception:
            prior_created_at = None
    task_file = task_file.resolve()
    candidates = (
        load_c1_tasks(task_file)
        if split == "formal"
        else select_pilot_tasks(task_file, cohort=pilot_cohort)
    )
    eligible, qualification = qualify_tasks(
        candidates,
        output_dir=root,
        headed=headed,
        allow_single_image=split == "pilot",
        allow_pilot_fallback=(
            split == "pilot" and pilot_cohort == "development"
        ),
    )
    selected = (
        select_formal_tasks(eligible)
        if split == "formal"
        else eligible
    )
    expected_bases = 40 if split == "formal" else 10
    if len(selected) != expected_bases:
        raise ValueError(
            f"{split} hybrid dataset requires {expected_bases} bases, selected {len(selected)}"
        )
    bases: list[dict[str, Any]] = []
    qualification_by_task = {
        str(row["task_id"]): row for row in qualification if row.get("task_id")
    }
    for task in selected:
        qualification_row = qualification_by_task[task.task_id]
        if split == "pilot":
            reusable = _pilot_base_capture_reusable(
                task, root / "base-captures" / task.task_id
            )
            if (
                reusable is not None
                and reusable.get("reference_receipt", {}).get("capture_mode")
                == "registered_golden_pilot_fallback"
                and qualification_row.get("eligibility_mode")
                != "registered_golden_pilot_fallback"
            ):
                # Infrastructure recovered after a pilot fallback.  Replace the
                # synthetic base with a real capture instead of silently carrying
                # the old fallback into a newly successful qualification epoch.
                reusable = None
            if reusable is not None:
                if qualification_row.get("eligibility_mode") == "registered_golden_pilot_fallback":
                    reason = str(qualification_row.get("fallback_reason") or "")
                    reusable["reference_receipt"]["fallback_reason"] = reason
                    reusable["reference_receipt"]["programmatic"]["reason"] = reason
                    write_json(
                        root / "base-captures" / task.task_id / "base-receipt.json",
                        reusable,
                    )
                bases.append(reusable)
                write_json(root / "base-captures.json", {"bases": bases})
                continue
        if qualification_row.get("eligibility_mode") == "registered_golden_pilot_fallback":
            bases.append(
                _freeze_pilot_fallback_capture(
                    task,
                    output_dir=root,
                    fallback_reason=str(qualification_row.get("fallback_reason") or ""),
                )
            )
        else:
            bases.append(
                _freeze_base_capture(
                    task,
                    output_dir=root,
                    headed=headed,
                )
            )
        write_json(root / "base-captures.json", {"bases": bases})
    if split == "pilot":
        for base in bases:
            base["pilot_wrong_state_donor"] = _pilot_wrong_state_donor(base)
            write_json(
                root / "base-captures" / base["task_id"] / "base-receipt.json",
                base,
            )
        write_json(root / "base-captures.json", {"bases": bases})
    samples = build_samples_from_bases(bases, output_dir=root)
    expected_samples = expected_bases * len(FORMAL_VARIANTS)
    if len(samples) != expected_samples:
        raise ValueError(
            f"{split} hybrid dataset requires {expected_samples} samples, got {len(samples)}"
        )
    source_provenance = _source_provenance(Path.cwd())
    chromium_executable = Path(
        os.environ.get("WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE", "")
    ).expanduser()
    runtime_environment = {
        "controller_python": sys.executable,
        "controller_python_version": sys.version,
        "jupyterlab_server_python": os.environ.get(
            "WARPTEST_JUPYTERLAB_PYTHON"
        ),
        "playwright_chromium_executable": (
            str(chromium_executable.resolve())
            if chromium_executable.is_file()
            else None
        ),
        "playwright_chromium_sha256": (
            _sha256_file(chromium_executable)
            if chromium_executable.is_file()
            else None
        ),
    }
    manifest = {
        "schema": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "created_at_utc": prior_created_at or _utc_now(),
        "split": split,
        "formal": split == "formal",
        "pilot_cohort": pilot_cohort if split == "pilot" else None,
        "selection_salt": SELECTION_SALT,
        "calibration_golden_policy": CALIBRATION_GOLDEN_POLICY,
        "reference_kind": "vlm_consensus",
        "model_policy": {
            "primary_judge": {
                "requested": "qwen3-vl-flash@relay",
                "accepted_provider_reported_models": ["qwen3-vl-flash"],
                "request_timeout_s": 300,
            },
            "annotator_a": {
                "requested": "glm-5.3-flash@relay",
                "accepted_provider_reported_models": ["glm-x-preview-a"],
                "alias_attestation": "user_confirmed_zhipu_internal_mapping",
                "request_timeout_s": 300,
            },
            "annotator_b": {
                "requested": "doubao-seed-2-1-turbo@relay",
                "accepted_provider_reported_models": [
                    "doubao-seed-2-1-turbo-260628"
                ],
                "request_timeout_s": 600,
            },
            "adjudicator": {
                "requested": "claude-sonnet-5@relay",
                "accepted_provider_reported_models": ["claude-sonnet-5"],
                "temperature_policy": "omitted_model_contract",
                "request_timeout_s": 300,
            },
        },
        "cache_policy": {
            "result_cache": "disabled",
            "provider_prompt_prefix_cache": "allowed_and_reported",
            "one_valid_provider_call_per_sample": True,
        },
        "gate_policy": {
            "reference_kappa_min": 0.70,
            "clean_acceptance_min": 0.90,
            "fault_rejection_min": 0.90,
            "pure_fault_false_accept_max": 0.05,
            "wrong_state_false_accept": "reported_noncausal_not_gated",
        },
        "task_file": str(task_file),
        "task_file_sha256": _sha256_file(task_file),
        "p5_manifest_sha256": _sha256_file(
            Path("benchmarks/p5/manifest.yaml").resolve()
        ),
        "harness_sha256": _sha256_file(Path(__file__).resolve()),
        "source_provenance": source_provenance,
        "runtime_environment": runtime_environment,
        "expected_benchmarks": list(EXPECTED_BENCHMARKS),
        "base_task_count": len(bases),
        "sample_count": len(samples),
        "variants": list(FORMAL_VARIANTS),
        "intervention_policy": {
            "runtime_overlay": sorted(BROWSER_OVERLAY_BENCHMARKS),
            "post_render": sorted(POST_RENDER_BENCHMARKS),
        },
        "corruption": {
            "blank_render": {
                "inset_fraction": 0.04,
                "rgb": [8, 8, 8],
                "changed_pixel_fraction_min_exclusive": BLANK_CHANGED_MIN,
            },
            "critical_occlusion": {
                "geometry": "distributed_vertical_stripes_v1",
                "normalized_box": [0.0, 0.0, 1.0, 1.0],
                "stripe_width_fraction": 0.015625,
                "covered_fraction": 0.6666666666666666,
                "rgb": [20, 20, 20],
                "changed_pixel_fraction_range": [
                    OCCLUSION_CHANGED_MIN,
                    OCCLUSION_CHANGED_MAX,
                ],
            },
        },
        "statistics": {
            "bootstrap_iterations": BOOTSTRAP_ITERATIONS,
            "bootstrap_seed": BOOTSTRAP_SEED,
            "cluster": "base_task_id",
        },
        "pilot_cohort_limitations": (
            {
                "reused_tasks": ["pilot-blender-save-reopen-scene"],
                "reason": "Only two non-formal Blender visual specs exist; v1 used the other task.",
            }
            if split == "pilot" and pilot_cohort == "confirmation"
            else {"reused_tasks": []}
        ),
        "qualification": {
            "candidate_count": len(candidates),
            "eligible_count": sum(bool(row.get("eligible")) for row in qualification),
            "pilot_fallback_count": sum(
                row.get("eligibility_mode") == "registered_golden_pilot_fallback"
                for row in qualification
            ),
            "audit_path": str((root / "qualification-audit.json").resolve()),
        },
        "bases": [
            {
                "task_id": base["task_id"],
                "benchmark": base["benchmark"],
                "intervention_level": base["intervention_level"],
                "state_projection_sha256": base["state_projection"]["sha256"],
            }
            for base in bases
        ],
        "samples": samples,
    }
    identity_payload = {
        "schema": manifest["schema"],
        "schema_version": manifest["schema_version"],
        "split": manifest["split"],
        "formal": manifest["formal"],
        "selection_salt": manifest["selection_salt"],
        "calibration_golden_policy": manifest["calibration_golden_policy"],
        "reference_kind": manifest["reference_kind"],
        "model_policy": manifest["model_policy"],
        "cache_policy": manifest["cache_policy"],
        "gate_policy": manifest["gate_policy"],
        "task_file_sha256": manifest["task_file_sha256"],
        "p5_manifest_sha256": manifest["p5_manifest_sha256"],
        "harness_sha256": manifest["harness_sha256"],
        "source_provenance": manifest["source_provenance"],
        "runtime_environment": manifest["runtime_environment"],
        "intervention_policy": manifest["intervention_policy"],
        "corruption": manifest["corruption"],
        "statistics": manifest["statistics"],
        "pilot_cohort": manifest["pilot_cohort"],
        "pilot_cohort_limitations": manifest["pilot_cohort_limitations"],
        "bases": manifest["bases"],
        "samples": [
            {
                key: sample.get(key)
                for key in (
                    "sample_id",
                    "base_task_id",
                    "benchmark",
                    "stage",
                    "variant",
                    "design_label",
                    "description",
                    "rubric",
                    "golden_sha256",
                    "live_sha256",
                    "intervention_level",
                    "programmatic_pass",
                    "state_projection_sha256",
                    "wrong_state_donor_task_id",
                    "wrong_state_distance",
                )
            }
            for sample in manifest["samples"]
        ],
    }
    manifest["dataset_id"] = _json_hash(identity_payload)
    manifest["manifest_sha256"] = _json_hash(manifest)
    write_json(root / "visual-calibration-manifest.json", manifest)
    return root


def load_hybrid_manifest(dataset_dir: Path) -> dict[str, Any]:
    path = dataset_dir / "visual-calibration-manifest.json"
    manifest = _load_mapping(path)
    supported = {
        LEGACY_PROTOCOL: 1,
        PROTOCOL_V2: 2,
        PROTOCOL: SCHEMA_VERSION,
    }
    schema = manifest.get("schema")
    if schema not in supported or manifest.get("schema_version") != supported[schema]:
        raise ValueError(f"Unsupported hybrid visual calibration manifest: {path}")
    samples = manifest.get("samples")
    if not isinstance(samples, list):
        raise ValueError("Hybrid visual calibration manifest has no samples")
    seen: set[str] = set()
    for sample in samples:
        if not isinstance(sample, dict):
            raise ValueError("Hybrid sample must be a mapping")
        sample_id = str(sample.get("sample_id") or "")
        if not sample_id or sample_id in seen:
            raise ValueError(f"Invalid or duplicate hybrid sample id: {sample_id!r}")
        seen.add(sample_id)
        for key in ("golden", "live"):
            path_value = Path(str(sample.get(key) or ""))
            if not path_value.is_file():
                raise ValueError(f"Hybrid sample {sample_id} lacks {key}: {path_value}")
            expected = sample.get(f"{key}_sha256")
            if expected != _sha256_file(path_value):
                raise ValueError(f"Hybrid sample {sample_id} {key} hash mismatch")
    return manifest


def _validate_formal_runtime_provenance(manifest: Mapping[str, Any]) -> None:
    if manifest.get("formal") is not True:
        return
    provenance = manifest.get("source_provenance")
    if not isinstance(provenance, Mapping):
        raise ValueError("Formal hybrid manifest lacks source provenance")
    source_files = provenance.get("source_files")
    if not isinstance(source_files, Mapping) or not source_files:
        raise ValueError("Formal hybrid manifest lacks source-file hashes")
    for relative, expected in source_files.items():
        path = Path(str(relative)).resolve()
        if not path.is_file() or _sha256_file(path) != expected:
            raise ValueError(f"Formal hybrid source provenance drift: {relative}")
    for field, path in (
        ("task_file_sha256", Path(str(manifest.get("task_file") or ""))),
        ("p5_manifest_sha256", Path("benchmarks/p5/manifest.yaml").resolve()),
    ):
        expected = manifest.get(field)
        if not path.is_file() or _sha256_file(path) != expected:
            raise ValueError(f"Formal hybrid manifest provenance drift: {field}")
    runtime_environment = manifest.get("runtime_environment")
    if not isinstance(runtime_environment, Mapping):
        raise ValueError("Formal hybrid manifest lacks runtime environment")
    if Path(sys.executable).resolve() != Path(
        str(runtime_environment.get("controller_python") or "")
    ).resolve():
        raise ValueError("Formal hybrid controller Python drift")
    if os.environ.get("WARPTEST_JUPYTERLAB_PYTHON") != runtime_environment.get(
        "jupyterlab_server_python"
    ):
        raise ValueError("Formal hybrid JupyterLab Python drift")
    expected_browser = runtime_environment.get("playwright_chromium_executable")
    current_browser = os.environ.get("WARPTEST_PLAYWRIGHT_CHROMIUM_EXECUTABLE")
    if current_browser != expected_browser:
        raise ValueError("Formal hybrid Playwright Chromium path drift")
    if expected_browser and _sha256_file(Path(str(expected_browser))) != runtime_environment.get(
        "playwright_chromium_sha256"
    ):
        raise ValueError("Formal hybrid Playwright Chromium binary drift")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"JSONL row {number} in {path} is not an object")
        rows.append(value)
    return rows


def _validate_provider_model_policy(
    manifest: Mapping[str, Any],
    *,
    policy_key: str,
    usage: Mapping[str, Any],
) -> list[str]:
    model_policy = manifest.get("model_policy")
    if not isinstance(model_policy, Mapping):
        return []
    policy = model_policy.get(policy_key)
    if not isinstance(policy, Mapping):
        raise ValueError(f"Hybrid manifest lacks model policy {policy_key}")
    allowed = {
        str(value)
        for value in policy.get("accepted_provider_reported_models", [])
        if str(value)
    }
    reported = {
        str(call.get("provider_reported_model"))
        for call in usage.get("calls", [])
        if isinstance(call, Mapping) and call.get("provider_reported_model")
    }
    if not reported or not allowed or not reported.issubset(allowed):
        raise ValueError(
            f"Hybrid provider model identity mismatch for {policy_key}: "
            f"reported={sorted(reported)!r}, allowed={sorted(allowed)!r}"
        )
    return sorted(reported)


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(_canonical_json(dict(row)) + "\n" for row in rows)
    path.write_text(body, encoding="utf-8")


@contextlib.contextmanager
def _relay_visual_model_environment(
    p5_manifest: Path,
    credentials_file: Path,
    *,
    model_spec: str,
) -> Iterable[dict[str, Any]]:
    manifest = _load_mapping(p5_manifest)
    profiles = manifest.get("model_request_profiles")
    if not isinstance(profiles, dict):
        raise ValueError("P5 manifest has no model_request_profiles mapping")
    model_spec = str(model_spec).strip()
    if not model_spec:
        raise ValueError("Visual model id must be non-empty")
    if "@" not in model_spec:
        model_spec = f"{model_spec}@relay"
    clean_model, _, provider_tag = model_spec.rpartition("@")
    if not clean_model or provider_tag.lower() != "relay":
        raise ValueError(
            "Hybrid visual calibration VLM routes must use the Relay provider"
        )
    previous_model = os.environ.get("WARPTEST_VISUAL_JUDGE_MODEL")
    previous_cache = os.environ.get("WARPTEST_C1_DISABLE_VISUAL_CACHE")
    os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] = model_spec
    os.environ["WARPTEST_C1_DISABLE_VISUAL_CACHE"] = "1"
    try:
        if os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE"):
            with model_request_profiles_environment(profiles):
                yield {
                    "model": model_spec,
                    "provider_model": clean_model,
                    "provider": "relay",
                    "fake": True,
                    "request_profile": model_request_profile_provenance(clean_model),
                    "result_cache": "disabled",
                    "temperature_policy": (
                        "omitted_model_contract"
                        if clean_model == "claude-sonnet-5"
                        else "fixed_zero"
                    ),
                }
        else:
            with relay_environment(credentials_file) as provider:
                with model_request_profiles_environment(profiles):
                    yield {
                        "model": model_spec,
                        "provider_model": clean_model,
                        "provider": "relay",
                        "fake": False,
                        "base_url": provider.base_url,
                        "request_profile": model_request_profile_provenance(
                            clean_model
                        ),
                        "result_cache": "disabled",
                        "temperature_policy": (
                            "omitted_model_contract"
                            if clean_model == "claude-sonnet-5"
                            else "fixed_zero"
                        ),
                    }
    finally:
        if previous_model is None:
            os.environ.pop("WARPTEST_VISUAL_JUDGE_MODEL", None)
        else:
            os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] = previous_model
        if previous_cache is None:
            os.environ.pop("WARPTEST_C1_DISABLE_VISUAL_CACHE", None)
        else:
            os.environ["WARPTEST_C1_DISABLE_VISUAL_CACHE"] = previous_cache


@contextlib.contextmanager
def _visual_judge_environment(
    p5_manifest: Path,
    credentials_file: Path,
) -> Iterable[dict[str, Any]]:
    manifest = _load_mapping(p5_manifest)
    visual = manifest.get("visual_judge")
    if not isinstance(visual, dict):
        raise ValueError("P5 manifest has no visual_judge mapping")
    model = str(visual.get("model") or manifest.get("visual_judge_model") or "")
    if model != "qwen3-vl-flash@relay":
        raise ValueError(
            "hybrid-visual-calibration-v1 is frozen to qwen3-vl-flash@relay; "
            f"manifest registers {model!r}"
        )
    with _relay_visual_model_environment(
        p5_manifest,
        credentials_file,
        model_spec=model,
    ) as identity:
        yield identity


def judge_hybrid_dataset(
    *,
    dataset_dir: Path,
    p5_manifest: Path = Path("benchmarks/p5/manifest.yaml"),
    credentials_file: Path = Path("test_model_batch.py"),
) -> Path:
    dataset_dir = dataset_dir.resolve()
    manifest = load_hybrid_manifest(dataset_dir)
    _validate_formal_runtime_provenance(manifest)
    results_path = dataset_dir / "judge-results.jsonl"
    prior = {str(row.get("sample_id")): row for row in _read_jsonl(results_path)}
    results = [prior[sample_id] for sample_id in prior]
    with _visual_judge_environment(p5_manifest.resolve(), credentials_file.resolve()) as identity:
        client = VisualJudgeClient.from_env()
        for sample in manifest["samples"]:
            sample_id = sample["sample_id"]
            if sample_id in prior:
                continue
            golden = Path(sample["golden"])
            live = Path(sample["live"])
            prompt = build_pair_judge_prompt(
                stage=str(sample["stage"]),
                description=str(sample["description"]),
                rubric=sample["rubric"],
                expected_label="correct",
            )
            with cost.scoped_meter() as meter:
                verdict = client.judge(
                    prompt=prompt,
                    live_screenshot=live,
                    golden_image=golden,
                )
            usage = meter.summary()
            expected_calls = 0 if identity.get("fake") else 1
            if usage["llm_calls"] != expected_calls or not usage["usage_complete"]:
                raise ValueError(
                    f"Hybrid judge sample {sample_id} lacks the expected complete call receipt"
                )
            if not identity.get("fake"):
                _validate_provider_model_policy(
                    manifest,
                    policy_key="primary_judge",
                    usage=usage,
                )
            exact = _normalized_image_equality(live, golden)
            row = {
                "schema": PROTOCOL,
                "schema_version": SCHEMA_VERSION,
                "sample_id": sample_id,
                "dataset_id": manifest["dataset_id"],
                "judge_model": verdict.get("judge_model"),
                "judge_label": verdict["label"],
                "pipeline_label": "correct" if exact["match"] else verdict["label"],
                "pipeline_source": (
                    "normalized_image_equality_v1" if exact["match"] else "llm_visual_judge"
                ),
                "confidence": verdict.get("confidence"),
                "observed": verdict.get("observed"),
                "rationale": verdict.get("rationale"),
                "rubric_failures": verdict.get("rubric_failures", []),
                "latency_ms": verdict.get("latency_ms"),
                "prompt_sha256": _sha256_text(prompt),
                "usage": usage,
                "cache_hit": False,
                "identity": identity,
                "completed_at_utc": _utc_now(),
            }
            results.append(row)
            results.sort(key=lambda item: str(item["sample_id"]))
            _write_jsonl(results_path, results)
    return results_path


def annotation_order(dataset_id: str, annotator_id: str, samples: Sequence[Mapping[str, Any]]) -> list[str]:
    return [
        str(item["sample_id"])
        for item in sorted(
            samples,
            key=lambda item: _sha256_text(
                f"{dataset_id}|{annotator_id}|{item['sample_id']}"
            ),
        )
    ]


def _annotation_path(dataset_dir: Path, annotator_id: str) -> Path:
    return dataset_dir / "annotations" / f"{annotator_id}.jsonl"


def _finalization_path(dataset_dir: Path, annotator_id: str) -> Path:
    return dataset_dir / "annotations" / f"{annotator_id}.finalization.json"


def _vlm_receipts_path(dataset_dir: Path, annotator_id: str) -> Path:
    return dataset_dir / "annotations" / f"{annotator_id}.vlm-receipts.jsonl"


def _vlm_errors_path(dataset_dir: Path, annotator_id: str) -> Path:
    return dataset_dir / "annotations" / f"{annotator_id}.vlm-errors.jsonl"


def _annotation_map(dataset_dir: Path, annotator_id: str) -> dict[str, dict[str, Any]]:
    return {
        str(row["sample_id"]): row
        for row in _read_jsonl(_annotation_path(dataset_dir, annotator_id))
    }


def _vlm_annotation_row(
    receipt: Mapping[str, Any],
    sample: Mapping[str, Any],
) -> dict[str, Any]:
    raw_failures = [str(item) for item in receipt.get("rubric_failures_raw", [])]
    allowed_failures = [
        *[str(item) for item in sample["rubric"].get("must", [])],
        *[str(item) for item in sample["rubric"].get("must_not", [])],
    ]
    failures = [item for item in allowed_failures if item in raw_failures]
    note = str(receipt.get("rationale") or receipt.get("observed") or "").strip()
    label = str(receipt["label"])
    if label in {"incorrect", "uncertain"} and not note:
        raise ValueError(
            f"VLM annotation {receipt.get('sample_id')} lacks the required rationale"
        )
    return {
        "schema": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": receipt["dataset_id"],
        "annotator_id": receipt["annotator_id"],
        "sample_id": receipt["sample_id"],
        "label": label,
        "rubric_failures": failures,
        "note": note,
        "annotation_source": "vlm",
        "annotator_model": receipt["annotator_model"],
        "receipt_sha256": _json_hash(dict(receipt)),
        "updated_at_utc": receipt["completed_at_utc"],
    }


def annotate_hybrid_dataset_with_vlm(
    *,
    dataset_dir: Path,
    annotator_id: str,
    model_spec: str | None = None,
    adjudicate: bool = False,
    p5_manifest: Path = Path("benchmarks/p5/manifest.yaml"),
    credentials_file: Path = Path("test_model_batch.py"),
) -> Path:
    """Blindly label every frozen sample with one explicitly identified VLM.

    The per-call receipts are the resumable paid evidence.  The compact
    ``annotator-*.jsonl`` files are deterministically materialized from those
    receipts so a crash cannot force a completed sample to be purchased again.
    """

    dataset_dir = dataset_dir.resolve()
    manifest = load_hybrid_manifest(dataset_dir)
    _validate_formal_runtime_provenance(manifest)
    if adjudicate:
        if annotator_id != ADJUDICATOR_ID:
            raise ValueError(f"VLM adjudication must use annotator id {ADJUDICATOR_ID}")
        if not model_spec:
            raise ValueError("VLM adjudication requires an explicit independent model")
    elif annotator_id not in ANNOTATOR_IDS:
        raise ValueError(f"Primary annotation id must be one of {ANNOTATOR_IDS}")
    selected_model = str(
        model_spec
        or DEFAULT_VLM_ANNOTATOR_MODELS.get(annotator_id)
        or ""
    ).strip()
    if "@" not in selected_model:
        selected_model = f"{selected_model}@relay"
    if selected_model.rpartition("@")[2].lower() != "relay":
        raise ValueError("VLM annotators must use an @relay model route")

    annotation_path = _annotation_path(dataset_dir, annotator_id)
    receipts_path = _vlm_receipts_path(dataset_dir, annotator_id)
    errors_path = _vlm_errors_path(dataset_dir, annotator_id)
    finalization_path = _finalization_path(dataset_dir, annotator_id)
    if finalization_path.exists():
        finalization = _load_mapping(finalization_path)
        if (
            finalization.get("annotation_source") != "vlm"
            or finalization.get("annotator_model") != selected_model
            or finalization.get("dataset_id") != manifest["dataset_id"]
        ):
            raise ValueError(
                f"Existing finalized annotation set for {annotator_id} has a different source or model"
            )
        if finalization.get("annotations_sha256") != _sha256_file(annotation_path):
            raise ValueError(f"Finalized annotations changed after lock: {annotator_id}")
        if finalization.get("receipts_sha256") != _sha256_file(receipts_path):
            raise ValueError(f"Finalized VLM receipts changed after lock: {annotator_id}")
        return annotation_path

    samples = {str(sample["sample_id"]): sample for sample in manifest["samples"]}
    selected_ids = (
        set(_adjudication_ids(dataset_dir, manifest))
        if adjudicate
        else set(samples)
    )
    full_order = annotation_order(
        str(manifest["dataset_id"]), annotator_id, manifest["samples"]
    )
    order = [sample_id for sample_id in full_order if sample_id in selected_ids]
    if adjudicate:
        primary_models = {
            str(
                _load_annotation_finalization(dataset_dir, primary_id).get(
                    "annotator_model"
                )
                or ""
            )
            for primary_id in ANNOTATOR_IDS
        }
        forbidden = {"qwen3-vl-flash@relay", *primary_models}
        if selected_model in forbidden:
            raise ValueError(
                "VLM adjudicator must be independent of the primary judge and annotators"
            )
    prior_rows = _read_jsonl(receipts_path)
    error_rows = _read_jsonl(errors_path)
    if manifest.get("formal") is True and error_rows:
        raise ValueError(
            f"Formal VLM annotator {annotator_id} has prior failed or unknown attempts"
        )
    prior_ids = [str(row.get("sample_id") or "") for row in prior_rows]
    if len(prior_ids) != len(set(prior_ids)):
        raise ValueError(f"Duplicate VLM receipts for {annotator_id}")
    receipts = {sample_id: row for sample_id, row in zip(prior_ids, prior_rows)}
    existing_annotations = _annotation_map(dataset_dir, annotator_id)
    if existing_annotations and not receipts:
        raise ValueError(
            f"Existing non-VLM annotations for {annotator_id} cannot be overwritten"
        )

    def validate_receipt(sample_id: str, receipt: Mapping[str, Any]) -> None:
        sample = samples.get(sample_id)
        if sample is None:
            raise ValueError(f"VLM receipt references an unknown sample: {sample_id}")
        if receipt.get("schema") != VLM_ANNOTATION_SCHEMA:
            raise ValueError(f"VLM receipt {sample_id} has the wrong schema")
        if receipt.get("dataset_id") != manifest["dataset_id"]:
            raise ValueError(f"VLM receipt {sample_id} has the wrong dataset id")
        if receipt.get("annotator_id") != annotator_id:
            raise ValueError(f"VLM receipt {sample_id} has the wrong annotator id")
        if receipt.get("annotator_model") != selected_model:
            raise ValueError(f"VLM receipt {sample_id} has the wrong model identity")
        usage = receipt.get("usage")
        if not isinstance(usage, Mapping) or usage.get("usage_complete") is not True:
            raise ValueError(f"VLM receipt {sample_id} has incomplete usage")
        expected_calls = 0 if receipt.get("identity", {}).get("fake") else 1
        if usage.get("llm_calls") != expected_calls:
            raise ValueError(f"VLM receipt {sample_id} has the wrong call count")
        prompt = build_pair_judge_prompt(
            stage=str(sample["stage"]),
            description=str(sample["description"]),
            rubric=sample["rubric"],
            expected_label="correct",
        )
        if receipt.get("prompt_sha256") != _sha256_text(prompt):
            raise ValueError(f"VLM receipt {sample_id} has a prompt hash mismatch")

    for sample_id, receipt in receipts.items():
        validate_receipt(sample_id, receipt)

    def persist() -> None:
        ordered_receipts = [receipts[sample_id] for sample_id in order if sample_id in receipts]
        _write_jsonl(receipts_path, ordered_receipts)
        annotations = [
            _vlm_annotation_row(receipts[sample_id], samples[sample_id])
            for sample_id in order
            if sample_id in receipts
        ]
        _write_jsonl(annotation_path, annotations)

    if receipts:
        persist()

    with _relay_visual_model_environment(
        p5_manifest.resolve(),
        credentials_file.resolve(),
        model_spec=selected_model,
    ) as identity:
        if manifest.get("formal") is True and identity.get("fake") is not False:
            raise ValueError("Formal VLM annotations cannot use the fake visual judge")
        client = VisualJudgeClient.from_env()
        for sample_id in order:
            if sample_id in receipts:
                continue
            sample = samples[sample_id]
            prompt = build_pair_judge_prompt(
                stage=str(sample["stage"]),
                description=str(sample["description"]),
                rubric=sample["rubric"],
                expected_label="correct",
            )
            try:
                with cost.scoped_meter() as meter:
                    verdict = client.judge(
                        prompt=prompt,
                        live_screenshot=Path(sample["live"]),
                        golden_image=Path(sample["golden"]),
                    )
            except Exception as error:
                usage = meter.summary()
                error_row = {
                    "schema": VLM_ANNOTATION_SCHEMA,
                    "schema_version": 1,
                    "dataset_id": manifest["dataset_id"],
                    "annotator_id": annotator_id,
                    "annotation_role": (
                        "adjudicator" if adjudicate else "primary"
                    ),
                    "annotator_model": selected_model,
                    "sample_id": sample_id,
                    "prompt_sha256": _sha256_text(prompt),
                    "error_type": type(error).__name__,
                    "error": str(error),
                    "usage": usage,
                    "identity": identity,
                    "failed_at_utc": _utc_now(),
                }
                error_row["error_receipt_sha256"] = _json_hash(error_row)
                error_rows.append(error_row)
                _write_jsonl(errors_path, error_rows)
                raise
            usage = meter.summary()
            expected_calls = 0 if identity.get("fake") else 1
            if usage["llm_calls"] != expected_calls or not usage["usage_complete"]:
                raise ValueError(
                    f"VLM annotation sample {sample_id} lacks the expected complete call receipt"
                )
            policy_key = (
                "adjudicator"
                if adjudicate
                else "annotator_a"
                if annotator_id == ANNOTATOR_IDS[0]
                else "annotator_b"
            )
            provider_reported_models = (
                []
                if identity.get("fake")
                else _validate_provider_model_policy(
                    manifest,
                    policy_key=policy_key,
                    usage=usage,
                )
            )
            receipt = {
                "schema": VLM_ANNOTATION_SCHEMA,
                "schema_version": 1,
                "dataset_id": manifest["dataset_id"],
                "annotator_id": annotator_id,
                "annotation_role": "adjudicator" if adjudicate else "primary",
                "annotator_model": selected_model,
                "sample_id": sample_id,
                "label": verdict["label"],
                "confidence": verdict.get("confidence"),
                "observed": verdict.get("observed"),
                "rationale": verdict.get("rationale"),
                "rubric_failures_raw": verdict.get("rubric_failures", []),
                "latency_ms": verdict.get("latency_ms"),
                "prompt_sha256": _sha256_text(prompt),
                "usage": usage,
                "provider_reported_models": provider_reported_models,
                "result_cache_hit": False,
                "identity": identity,
                "blind_input_contract": {
                    "included": [
                        "golden_image",
                        "live_image",
                        "stage",
                        "description",
                        "rubric.must",
                        "rubric.must_not",
                        "rubric.ignore",
                    ],
                    "excluded": [
                        "benchmark",
                        "base_task_id",
                        "variant",
                        "design_label",
                        "other_annotator_label",
                        *(["primary_annotator_labels"] if adjudicate else []),
                        "qwen_judge_result",
                    ],
                },
                "completed_at_utc": _utc_now(),
            }
            validate_receipt(sample_id, receipt)
            receipts[sample_id] = receipt
            persist()

    if len(receipts) != len(order):
        raise ValueError(
            f"VLM annotator {annotator_id} completed {len(receipts)}/{len(order)} samples"
        )
    payload = {
        "schema": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "dataset_id": manifest["dataset_id"],
        "annotator_id": annotator_id,
        "annotation_role": "adjudicator" if adjudicate else "primary",
        "annotation_source": "vlm",
        "annotator_model": selected_model,
        "request_profile": (
            next(iter(receipts.values()))["identity"].get("request_profile")
            if receipts
            else model_request_profile_provenance(
                selected_model.rpartition("@")[0]
            )
        ),
        "sample_count": len(order),
        "annotations_sha256": _sha256_file(annotation_path),
        "receipts_sha256": _sha256_file(receipts_path),
        "usage_complete": all(
            receipt["usage"].get("usage_complete") is True
            for receipt in receipts.values()
        ),
        "llm_calls": sum(
            int(receipt["usage"].get("llm_calls") or 0)
            for receipt in receipts.values()
        ),
        "provider_reported_models": sorted(
            {
                model
                for receipt in receipts.values()
                for model in receipt.get("provider_reported_models", [])
            }
        ),
        "error_attempt_count": len(error_rows),
        "errors_sha256": _sha256_file(errors_path) if error_rows else None,
        "finalized_at_utc": _utc_now(),
    }
    payload["finalization_sha256"] = _json_hash(payload)
    write_json(finalization_path, payload)
    return annotation_path


def _adjudication_ids(dataset_dir: Path, manifest: Mapping[str, Any]) -> list[str]:
    first = _load_finalized_annotations(dataset_dir, ANNOTATOR_IDS[0])
    second = _load_finalized_annotations(dataset_dir, ANNOTATOR_IDS[1])
    missing = [
        sample["sample_id"]
        for sample in manifest["samples"]
        if sample["sample_id"] not in first or sample["sample_id"] not in second
    ]
    if missing:
        raise ValueError("Both primary annotators must finish before adjudication")
    return [
        sample["sample_id"]
        for sample in manifest["samples"]
        if first[sample["sample_id"]]["label"]
        != second[sample["sample_id"]]["label"]
        or first[sample["sample_id"]]["label"] == "uncertain"
        or second[sample["sample_id"]]["label"] == "uncertain"
    ]


class HybridAnnotationSession:
    def __init__(self, *, dataset_dir: Path, annotator_id: str, adjudicate: bool = False) -> None:
        self.dataset_dir = dataset_dir.resolve()
        self.manifest = load_hybrid_manifest(self.dataset_dir)
        self.annotator_id = annotator_id
        self.adjudicate = adjudicate
        if adjudicate and annotator_id != ADJUDICATOR_ID:
            raise ValueError(f"Adjudication must use annotator id {ADJUDICATOR_ID}")
        if not adjudicate and annotator_id not in ANNOTATOR_IDS:
            raise ValueError(f"Primary annotation id must be one of {ANNOTATOR_IDS}")
        selected = (
            set(_adjudication_ids(self.dataset_dir, self.manifest))
            if adjudicate
            else {sample["sample_id"] for sample in self.manifest["samples"]}
        )
        ordered = annotation_order(
            str(self.manifest["dataset_id"]), annotator_id, self.manifest["samples"]
        )
        self.sample_ids = [sample_id for sample_id in ordered if sample_id in selected]
        self.samples = {sample["sample_id"]: sample for sample in self.manifest["samples"]}
        self.annotations = _annotation_map(self.dataset_dir, annotator_id)
        self.index = 0
        self.locked = _finalization_path(self.dataset_dir, annotator_id).exists()

    def state(self) -> dict[str, Any]:
        current_id = self.sample_ids[self.index] if self.sample_ids else None
        sample = self.samples.get(current_id) if current_id else None
        annotation = self.annotations.get(current_id) if current_id else None
        return {
            "complete": len(self.annotations) == len(self.sample_ids),
            "locked": self.locked,
            "index": self.index,
            "total": len(self.sample_ids),
            "rated": len(self.annotations),
            "can_previous": self.index > 0,
            "can_next": self.index < len(self.sample_ids) - 1,
            "current_sample": self._public_sample(sample) if sample else None,
            "current_annotation": annotation,
        }

    def _public_sample(self, sample: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "sample_id": sample["sample_id"],
            "title": f"Sample {self.index + 1}",
            "stage": sample["stage"],
            "description": sample["description"],
            "rubric": sample["rubric"],
            "golden": sample["golden"],
            "live": sample["live"],
        }

    def rate(
        self,
        *,
        label: str,
        rubric_failures: Sequence[str] | None = None,
        note: str = "",
    ) -> dict[str, Any]:
        if self.locked:
            raise ValueError("This annotation set is finalized and immutable")
        if label not in VISUAL_LABELS:
            raise ValueError("label must be correct, incorrect, or uncertain")
        if not self.sample_ids:
            raise ValueError("No samples require annotation")
        sample_id = self.sample_ids[self.index]
        sample = self.samples[sample_id]
        allowed_failures = [
            *sample["rubric"].get("must", []),
            *sample["rubric"].get("must_not", []),
        ]
        failures = [str(item) for item in rubric_failures or [] if str(item)]
        if any(item not in allowed_failures for item in failures):
            raise ValueError("rubric_failures contains an item not shown to the annotator")
        note = str(note).strip()
        if label == "incorrect" and not failures and not note:
            raise ValueError("incorrect without a rubric failure requires a visible-failure note")
        if label == "uncertain" and not note:
            raise ValueError("uncertain requires a reason")
        row = {
            "schema": PROTOCOL,
            "schema_version": SCHEMA_VERSION,
            "dataset_id": self.manifest["dataset_id"],
            "annotator_id": self.annotator_id,
            "sample_id": sample_id,
            "label": label,
            "rubric_failures": failures,
            "note": note,
            "updated_at_utc": _utc_now(),
        }
        self.annotations[sample_id] = row
        ordered_rows = [
            self.annotations[item]
            for item in self.sample_ids
            if item in self.annotations
        ]
        _write_jsonl(_annotation_path(self.dataset_dir, self.annotator_id), ordered_rows)
        return self.state()

    def next(self) -> dict[str, Any]:
        if self.index < len(self.sample_ids) - 1:
            self.index += 1
        return self.state()

    def previous(self) -> dict[str, Any]:
        if self.index > 0:
            self.index -= 1
        return self.state()

    def finalize(self) -> dict[str, Any]:
        if self.locked:
            return self.state()
        if len(self.annotations) != len(self.sample_ids):
            raise ValueError("Every assigned sample must be rated before finalization")
        path = _annotation_path(self.dataset_dir, self.annotator_id)
        payload = {
            "schema": PROTOCOL,
            "schema_version": SCHEMA_VERSION,
            "dataset_id": self.manifest["dataset_id"],
            "annotator_id": self.annotator_id,
            "annotation_source": "human",
            "annotator_model": None,
            "sample_count": len(self.sample_ids),
            "annotations_sha256": _sha256_file(path),
            "finalized_at_utc": _utc_now(),
        }
        payload["finalization_sha256"] = _json_hash(payload)
        write_json(_finalization_path(self.dataset_dir, self.annotator_id), payload)
        self.locked = True
        return self.state()


def _accuracy(predicted: Sequence[str], actual: Sequence[str]) -> float:
    if not actual:
        return 0.0
    return sum(left == right for left, right in zip(predicted, actual)) / len(actual)


def _cohen_kappa(left: Sequence[str], right: Sequence[str]) -> float:
    if len(left) != len(right):
        raise ValueError("Cohen kappa inputs differ in length")
    if not left:
        return 0.0
    observed = _accuracy(left, right)
    expected = 0.0
    for label in ("correct", "incorrect", "uncertain"):
        expected += (left.count(label) / len(left)) * (right.count(label) / len(right))
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def _confusion(actual: Sequence[str], predicted: Sequence[str]) -> dict[str, dict[str, int]]:
    labels = ("correct", "incorrect", "uncertain")
    matrix = {truth: {guess: 0 for guess in labels} for truth in labels}
    for truth, guess in zip(actual, predicted):
        matrix[truth][guess] += 1
    return matrix


def _macro_f1(actual: Sequence[str], predicted: Sequence[str]) -> float:
    values: list[float] = []
    for label in ("correct", "incorrect", "uncertain"):
        tp = sum(a == label and p == label for a, p in zip(actual, predicted))
        fp = sum(a != label and p == label for a, p in zip(actual, predicted))
        fn = sum(a == label and p != label for a, p in zip(actual, predicted))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        values.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return statistics.fmean(values)


def _rate_ci_by_cluster(
    rows: Sequence[Mapping[str, Any]],
    predicate,
    *,
    iterations: int = BOOTSTRAP_ITERATIONS,
) -> dict[str, Any]:
    if not rows:
        return {"n": 0, "clusters": 0, "mean": None, "lo": None, "hi": None}
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["base_task_id"]), []).append(row)
    names = sorted(grouped)
    mean = sum(bool(predicate(row)) for row in rows) / len(rows)
    rng = random.Random(BOOTSTRAP_SEED)
    values: list[float] = []
    for _ in range(iterations):
        sampled = [rng.choice(names) for _ in names]
        sample_rows = [row for name in sampled for row in grouped[name]]
        values.append(sum(bool(predicate(row)) for row in sample_rows) / len(sample_rows))
    values.sort()
    lo = values[math.floor(0.025 * (len(values) - 1))]
    hi = values[math.ceil(0.975 * (len(values) - 1))]
    return {"n": len(rows), "clusters": len(names), "mean": mean, "lo": lo, "hi": hi}


def _exact_mcnemar(first: Sequence[bool], second: Sequence[bool]) -> dict[str, Any]:
    if len(first) != len(second):
        raise ValueError("McNemar inputs differ in length")
    b = sum(left and not right for left, right in zip(first, second))
    c = sum(not left and right for left, right in zip(first, second))
    n = b + c
    if n == 0:
        p = 1.0
    else:
        tail = sum(math.comb(n, index) for index in range(0, min(b, c) + 1)) / (2**n)
        p = min(1.0, 2.0 * tail)
    return {"b": b, "c": c, "discordant": n, "p_value": p}


def _judge_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    gold = [str(row["gold_label"]) for row in rows]
    predicted = [str(row["judge_label"]) for row in rows]
    decisive = [row for row in rows if row["gold_label"] != "uncertain"]
    incorrect = [row for row in decisive if row["gold_label"] == "incorrect"]
    correct = [row for row in decisive if row["gold_label"] == "correct"]
    covered = [row for row in decisive if row["judge_label"] != "uncertain"]
    return {
        "n": len(rows),
        "decisive_gold": len(decisive),
        "confusion_matrix": _confusion(gold, predicted),
        "three_class_accuracy": _accuracy(predicted, gold),
        "strict_accuracy": _accuracy(
            [str(row["judge_label"]) for row in decisive],
            [str(row["gold_label"]) for row in decisive],
        ),
        "macro_f1": _macro_f1(gold, predicted),
        "kappa": _cohen_kappa(gold, predicted),
        "selective_coverage": len(covered) / len(decisive) if decisive else 0.0,
        "selective_accuracy": _accuracy(
            [row["judge_label"] for row in covered],
            [row["gold_label"] for row in covered],
        ),
        "false_accept_rate": (
            sum(row["judge_label"] == "correct" for row in incorrect) / len(incorrect)
            if incorrect
            else 0.0
        ),
        "false_reject_rate": (
            sum(row["judge_label"] == "incorrect" for row in correct) / len(correct)
            if correct
            else 0.0
        ),
        "uncertain_rate": sum(row["judge_label"] == "uncertain" for row in rows) / len(rows)
        if rows
        else 0.0,
    }


def _group_metrics(rows: Sequence[Mapping[str, Any]], key: str) -> dict[str, Any]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get(key) or "unknown"), []).append(row)
    return {name: _judge_metrics(items) for name, items in sorted(grouped.items())}


def _load_finalized_annotations(dataset_dir: Path, annotator_id: str) -> dict[str, dict[str, Any]]:
    finalization_path = _finalization_path(dataset_dir, annotator_id)
    if not finalization_path.is_file():
        raise ValueError(f"Annotation set is not finalized: {annotator_id}")
    payload = _load_mapping(finalization_path)
    annotations_path = _annotation_path(dataset_dir, annotator_id)
    if payload.get("annotations_sha256") != _sha256_file(annotations_path):
        raise ValueError(f"Finalized annotations changed after lock: {annotator_id}")
    if payload.get("annotation_source") == "vlm":
        receipts_path = _vlm_receipts_path(dataset_dir, annotator_id)
        if payload.get("receipts_sha256") != _sha256_file(receipts_path):
            raise ValueError(f"Finalized VLM receipts changed after lock: {annotator_id}")
        receipts = _read_jsonl(receipts_path)
        if len(receipts) != payload.get("sample_count"):
            raise ValueError(f"Finalized VLM receipt count mismatch: {annotator_id}")
        for receipt in receipts:
            if (
                receipt.get("annotator_model") != payload.get("annotator_model")
                or receipt.get("dataset_id") != payload.get("dataset_id")
            ):
                raise ValueError(f"Finalized VLM receipt identity mismatch: {annotator_id}")
            usage = receipt.get("usage")
            if not isinstance(usage, Mapping) or usage.get("usage_complete") is not True:
                raise ValueError(f"Finalized VLM receipt usage is incomplete: {annotator_id}")
    return _annotation_map(dataset_dir, annotator_id)


def _load_annotation_finalization(dataset_dir: Path, annotator_id: str) -> dict[str, Any]:
    path = _finalization_path(dataset_dir, annotator_id)
    if not path.is_file():
        raise ValueError(f"Annotation set is not finalized: {annotator_id}")
    payload = _load_mapping(path)
    payload.setdefault("annotation_source", "human")
    payload.setdefault("annotator_model", None)
    return payload


def aggregate_hybrid_dataset(*, dataset_dir: Path) -> Path:
    dataset_dir = dataset_dir.resolve()
    manifest = load_hybrid_manifest(dataset_dir)
    _validate_formal_runtime_provenance(manifest)
    judges = {
        str(row["sample_id"]): row
        for row in _read_jsonl(dataset_dir / "judge-results.jsonl")
    }
    first = _load_finalized_annotations(dataset_dir, ANNOTATOR_IDS[0])
    second = _load_finalized_annotations(dataset_dir, ANNOTATOR_IDS[1])
    primary_finalizations = [
        _load_annotation_finalization(dataset_dir, annotator_id)
        for annotator_id in ANNOTATOR_IDS
    ]
    primary_sources = {
        str(payload.get("annotation_source") or "human")
        for payload in primary_finalizations
    }
    if primary_sources == {"human"}:
        reference_kind = "human"
    elif primary_sources == {"vlm"}:
        reference_kind = "vlm_consensus"
    else:
        reference_kind = "mixed"
    adjudication_ids = _adjudication_ids(dataset_dir, manifest)
    adjudicated = (
        _load_finalized_annotations(dataset_dir, ADJUDICATOR_ID)
        if adjudication_ids
        else {}
    )
    adjudicator_finalization = (
        _load_annotation_finalization(dataset_dir, ADJUDICATOR_ID)
        if adjudication_ids
        else None
    )
    if reference_kind == "vlm_consensus" and adjudicator_finalization is not None:
        primary_models = {
            str(payload.get("annotator_model") or "")
            for payload in primary_finalizations
        }
        adjudicator_model = str(
            adjudicator_finalization.get("annotator_model") or ""
        )
        if (
            adjudicator_finalization.get("annotation_source") != "vlm"
            or not adjudicator_model
            or adjudicator_model in primary_models
            or adjudicator_model == "qwen3-vl-flash@relay"
        ):
            raise ValueError(
                "VLM-consensus adjudicator is not independent of the evaluated models"
            )
    rows: list[dict[str, Any]] = []
    missing_judge: list[str] = []
    judge_integrity_issues: list[str] = []
    for sample in manifest["samples"]:
        sample_id = sample["sample_id"]
        if sample_id not in judges:
            missing_judge.append(sample_id)
            continue
        judge = judges[sample_id]
        if judge.get("cache_hit") is not False:
            judge_integrity_issues.append(f"{sample_id}: cache policy is not disabled")
        identity = judge.get("identity")
        if not isinstance(identity, Mapping) or identity.get("model") != "qwen3-vl-flash@relay":
            judge_integrity_issues.append(f"{sample_id}: visual-judge identity mismatch")
        usage = judge.get("usage")
        if not isinstance(usage, Mapping) or usage.get("usage_complete") is not True:
            judge_integrity_issues.append(f"{sample_id}: provider usage is incomplete")
        if manifest.get("formal") is True:
            if isinstance(identity, Mapping) and identity.get("fake") is not False:
                judge_integrity_issues.append(f"{sample_id}: formal judge result is fake")
            if isinstance(usage, Mapping) and usage.get("llm_calls") != 1:
                judge_integrity_issues.append(
                    f"{sample_id}: formal judge result does not have exactly one call"
                )
        left = first[sample_id]["label"]
        right = second[sample_id]["label"]
        needs_adjudication = sample_id in adjudication_ids
        if needs_adjudication:
            if sample_id not in adjudicated:
                raise ValueError(f"Missing adjudication for {sample_id}")
            gold = adjudicated[sample_id]["label"]
            gold_source = "adjudicator"
        else:
            gold = left
            gold_source = "primary_agreement"
        rows.append(
            {
                **sample,
                "annotator_a": left,
                "annotator_b": right,
                "gold_label": gold,
                "gold_source": gold_source,
                "judge_label": judges[sample_id]["judge_label"],
                "pipeline_label": judges[sample_id]["pipeline_label"],
            }
        )
    labels_a = [first[sample["sample_id"]]["label"] for sample in manifest["samples"]]
    labels_b = [second[sample["sample_id"]]["label"] for sample in manifest["samples"]]
    controls = [row for row in rows if row["variant"] in {"before_clean", "after_clean"}]
    faults = [row for row in rows if row["variant"] in FAULT_VARIANTS]
    incorrect_gold = [row for row in rows if row["gold_label"] == "incorrect"]
    fault_incorrect_gold = [
        row
        for row in faults
        if row["gold_label"] == "incorrect"
    ]
    clean_acceptance = _rate_ci_by_cluster(
        controls, lambda row: row["judge_label"] == "correct"
    )
    fault_rejection = _rate_ci_by_cluster(
        faults, lambda row: row["judge_label"] == "incorrect"
    )
    false_accept = (
        sum(row["judge_label"] == "correct" for row in incorrect_gold)
        / len(incorrect_gold)
        if incorrect_gold
        else 0.0
    )
    pure_fault_false_accept = (
        sum(row["judge_label"] == "correct" for row in fault_incorrect_gold)
        / len(fault_incorrect_gold)
        if fault_incorrect_gold
        else 0.0
    )
    paired: dict[str, Any] = {}
    after_by_task = {
        row["base_task_id"]: row
        for row in rows
        if row["variant"] == "after_clean"
    }
    for variant in sorted(FAULT_VARIANTS):
        variant_by_task = {
            row["base_task_id"]: row for row in rows if row["variant"] == variant
        }
        tasks = sorted(set(after_by_task) & set(variant_by_task))
        paired[variant] = _exact_mcnemar(
            [after_by_task[task]["judge_label"] == "correct" for task in tasks],
            [variant_by_task[task]["judge_label"] == "correct" for task in tasks],
        )
        paired[variant]["tasks"] = len(tasks)
    summary = {
        "schema": PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": _utc_now(),
        "dataset_id": manifest["dataset_id"],
        "complete": (
            not missing_judge
            and not judge_integrity_issues
            and len(rows) == len(manifest["samples"])
        ),
        "coverage": {
            "expected_samples": len(manifest["samples"]),
            "aggregated_samples": len(rows),
            "missing_judge_results": missing_judge,
            "judge_integrity_issues": judge_integrity_issues,
            "adjudication_required": len(adjudication_ids),
            "adjudicated": len(adjudicated),
            "uncertain_gold": sum(row["gold_label"] == "uncertain" for row in rows),
        },
        "reference_annotation": {
            "kind": reference_kind,
            "raw_agreement": _accuracy(labels_a, labels_b),
            "cohen_kappa": _cohen_kappa(labels_a, labels_b),
            "disagreements": sum(left != right for left, right in zip(labels_a, labels_b)),
            "uncertain_primary": sum(
                left == "uncertain" or right == "uncertain"
                for left, right in zip(labels_a, labels_b)
            ),
            "annotators": [
                {
                    "annotator_id": payload.get("annotator_id"),
                    "annotation_source": payload.get("annotation_source"),
                    "annotator_model": payload.get("annotator_model"),
                    "request_profile": payload.get("request_profile"),
                    "sample_count": payload.get("sample_count"),
                    "annotations_sha256": payload.get("annotations_sha256"),
                    "receipts_sha256": payload.get("receipts_sha256"),
                }
                for payload in primary_finalizations
            ],
            "adjudicator": (
                {
                    "annotator_id": adjudicator_finalization.get("annotator_id"),
                    "annotation_source": adjudicator_finalization.get(
                        "annotation_source"
                    ),
                    "annotator_model": adjudicator_finalization.get(
                        "annotator_model"
                    ),
                    "request_profile": adjudicator_finalization.get(
                        "request_profile"
                    ),
                    "sample_count": adjudicator_finalization.get("sample_count"),
                    "annotations_sha256": adjudicator_finalization.get(
                        "annotations_sha256"
                    ),
                    "receipts_sha256": adjudicator_finalization.get(
                        "receipts_sha256"
                    ),
                }
                if adjudicator_finalization is not None
                else None
            ),
        },
        "judge": {
            "overall": _judge_metrics(rows),
            "by_benchmark": _group_metrics(rows, "benchmark"),
            "by_variant": _group_metrics(rows, "variant"),
            "by_intervention_level": _group_metrics(rows, "intervention_level"),
        },
        "hybrid_causal": {
            "programmatic_fault_acceptance": _rate_ci_by_cluster(
                faults, lambda row: bool(row["programmatic_pass"])
            ),
            "visual_fault_rejection": fault_rejection,
            "hybrid_fault_rejection": fault_rejection,
            "clean_control_acceptance": clean_acceptance,
            "paired_exact_mcnemar": paired,
        },
        "claim_gates": {
            "reference_kappa_at_least_0_70": _cohen_kappa(labels_a, labels_b) >= 0.70,
            "clean_acceptance_at_least_0_90": (clean_acceptance["mean"] or 0.0) >= 0.90,
            "fault_rejection_at_least_0_90": (fault_rejection["mean"] or 0.0) >= 0.90,
            "pure_fault_false_accept_at_most_0_05": pure_fault_false_accept
            <= 0.05,
        },
        "reported_noncausal_wrong_state_inclusive_false_accept": false_accept,
        "rows": rows,
    }
    summary["paper_ready"] = summary["complete"] and all(summary["claim_gates"].values())
    output = dataset_dir / "visual-calibration-summary.json"
    write_json(output, summary)
    (dataset_dir / "visual-calibration-summary.md").write_text(
        _summary_markdown(summary), encoding="utf-8"
    )
    return output


def _summary_markdown(summary: Mapping[str, Any]) -> str:
    reference = summary["reference_annotation"]
    judge = summary["judge"]["overall"]
    causal = summary["hybrid_causal"]
    lines = [
        "# Hybrid Visual Calibration",
        "",
        f"- Complete: `{summary['complete']}`",
        f"- Paper ready: `{summary['paper_ready']}`",
        f"- Dataset: `{summary['dataset_id']}`",
        f"- Reference kind: `{reference['kind']}`",
        f"- Reference raw agreement: `{reference['raw_agreement']:.4f}`",
        f"- Reference Cohen's kappa: `{reference['cohen_kappa']:.4f}`",
        f"- Judge strict accuracy: `{judge['strict_accuracy']:.4f}`",
        f"- Judge macro-F1: `{judge['macro_f1']:.4f}`",
        f"- Judge false-accept rate: `{judge['false_accept_rate']:.4f}`",
        f"- Judge false-reject rate: `{judge['false_reject_rate']:.4f}`",
        "",
        "## Hybrid causal contrast",
        "",
        "| Condition | Mean | 95% CI | N |",
        "|---|---:|---:|---:|",
    ]
    for name, key in (
        ("Programmatic fault acceptance", "programmatic_fault_acceptance"),
        ("Visual fault rejection", "visual_fault_rejection"),
        ("Hybrid fault rejection", "hybrid_fault_rejection"),
        ("Clean control acceptance", "clean_control_acceptance"),
    ):
        value = causal[key]
        lines.append(
            f"| {name} | {value['mean']!s} | [{value['lo']!s}, {value['hi']!s}] | {value['n']} |"
        )
    lines.extend(["", "## Claim gates", ""])
    for key, value in summary["claim_gates"].items():
        lines.append(f"- {key}: `{value}`")
    return "\n".join(lines) + "\n"


def run_annotation_server(
    *,
    dataset_dir: Path,
    annotator_id: str,
    adjudicate: bool = False,
) -> Path:
    session = HybridAnnotationSession(
        dataset_dir=dataset_dir,
        annotator_id=annotator_id,
        adjudicate=adjudicate,
    )
    port = find_free_port()
    server = ThreadingHTTPServer(("127.0.0.1", port), _hybrid_handler_for(session))
    url = f"http://127.0.0.1:{port}"
    print(f"hybrid_visual_annotation_url={url}", flush=True)
    print(f"hybrid_visual_dataset={session.dataset_dir}", flush=True)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return session.dataset_dir


def _hybrid_handler_for(session: HybridAnnotationSession):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/":
                self._html(_HYBRID_HTML)
            elif parsed.path == "/state":
                self._json(session.state())
            elif parsed.path == "/file":
                self._file(parse_qs(parsed.query).get("path", [""])[0])
            else:
                self.send_error(HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            try:
                if parsed.path == "/rate":
                    payload = self._read_json()
                    state = session.rate(
                        label=str(payload.get("label") or ""),
                        rubric_failures=payload.get("rubric_failures") or [],
                        note=str(payload.get("note") or ""),
                    )
                elif parsed.path == "/next":
                    state = session.next()
                elif parsed.path == "/previous":
                    state = session.previous()
                elif parsed.path == "/finalize":
                    state = session.finalize()
                else:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                self._json(state)
            except Exception as error:
                self._json({"error": str(error)}, status=500)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

        def _json(self, value: Mapping[str, Any], status: int = 200) -> None:
            data = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", "application/json; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _html(self, value: str) -> None:
            data = value.encode("utf-8")
            self.send_response(200)
            self.send_header("content-type", "text/html; charset=utf-8")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _file(self, raw: str) -> None:
            path = Path(unquote(raw)).expanduser().resolve()
            root = session.dataset_dir.resolve()
            if root not in path.parents or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                self.send_error(HTTPStatus.FORBIDDEN)
                return
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("content-type", "image/png")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


_HYBRID_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hybrid Visual Calibration</title>
<style>
body{font-family:system-ui;margin:0;background:#f6f8fa;color:#172033}header{background:#172033;color:white;padding:14px 22px;display:flex;justify-content:space-between}main{max-width:1400px;margin:auto;padding:18px}.panel{background:white;border:1px solid #d0d7de;border-radius:8px;padding:14px;margin-bottom:14px}.images{display:grid;grid-template-columns:1fr 1fr;gap:14px}.images img{width:100%;background:#111;border:1px solid #aaa}.rubric{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.rubric div{background:#f2f4f7;padding:8px;border-radius:6px}button{padding:9px 13px;margin:4px;border:1px solid #8c959f;border-radius:6px;background:white;font-weight:600}button.primary{background:#0969da;color:white}.failure{display:block;margin:5px}textarea{width:100%;min-height:70px}#error{color:#b42318}</style></head>
<body><header><strong>Hybrid Visual Calibration</strong><span id="progress"></span></header><main>
<section class="panel"><div id="sample"></div><div id="description"></div><div class="rubric" id="rubric"></div></section>
<section class="images"><div class="panel"><h3>Golden oracle</h3><img id="golden"></div><div class="panel"><h3>Live screenshot</h3><img id="live"></div></section>
<section class="panel"><h3>Your label</h3><div id="failures"></div><textarea id="note" placeholder="Required for uncertain, or for an incorrect visible failure not represented by the rubric."></textarea><div><button data-label="correct">Correct</button><button data-label="incorrect">Incorrect</button><button data-label="uncertain">Uncertain</button></div><div id="error"></div></section>
<section class="panel"><button id="previous">Previous</button><button id="next">Next</button><button class="primary" id="finalize">Finalize all labels</button></section>
</main><script>
let state=null;const $=s=>document.querySelector(s);async function api(path,opts={}){const r=await fetch(path,opts);const j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}const file=p=>'/file?path='+encodeURIComponent(p);const esc=v=>String(v).replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
function render(s){state=s;$('#progress').textContent=`${s.rated}/${s.total}`;const x=s.current_sample;if(!x){$('#sample').textContent='No samples require annotation.';return}$('#sample').textContent=`${x.title} · ${x.stage}`;$('#description').textContent=x.description;$('#golden').src=file(x.golden);$('#live').src=file(x.live);$('#rubric').innerHTML=['must','must_not','ignore'].map(k=>`<div><strong>${k}</strong><ul>${(x.rubric[k]||[]).map(v=>`<li>${esc(v)}</li>`).join('')}</ul></div>`).join('');const opts=[...(x.rubric.must||[]),...(x.rubric.must_not||[])];$('#failures').innerHTML=opts.map((v,i)=>`<label class="failure"><input type="checkbox" value="${i}"> ${esc(v)}</label>`).join('');document.querySelectorAll('#failures input').forEach((n,i)=>n.dataset.text=opts[i]);const a=s.current_annotation||{};$('#note').value=a.note||'';document.querySelectorAll('#failures input').forEach(n=>n.checked=(a.rubric_failures||[]).includes(n.dataset.text));$('#previous').disabled=!s.can_previous;$('#next').disabled=!s.can_next;$('#finalize').disabled=s.locked||!s.complete;$('#error').textContent=s.locked?'Finalized and locked.':''}
async function rate(label){try{const failures=[...document.querySelectorAll('#failures input:checked')].map(n=>n.dataset.text);render(await api('/rate',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({label,rubric_failures:failures,note:$('#note').value})}))}catch(e){$('#error').textContent=e.message}}
document.querySelectorAll('[data-label]').forEach(b=>b.onclick=()=>rate(b.dataset.label));$('#previous').onclick=async()=>render(await api('/previous',{method:'POST'}));$('#next').onclick=async()=>render(await api('/next',{method:'POST'}));$('#finalize').onclick=async()=>{if(confirm('Finalize and permanently lock these labels?'))render(await api('/finalize',{method:'POST'}))};api('/state').then(render);
</script></body></html>"""


def run_hybrid_visual_phase(
    *,
    phase: str,
    dataset_dir: Path | None,
    task_file: Path = Path("benchmarks/p5/c1_tasks.yaml"),
    split: str = "formal",
    headed: bool = True,
    pilot_cohort: str = "development",
    annotator_id: str | None = None,
    annotation_mode: str = "human",
    annotator_model: str | None = None,
    p5_manifest: Path = Path("benchmarks/p5/manifest.yaml"),
    credentials_file: Path = Path("test_model_batch.py"),
) -> Path:
    if phase == "prepare":
        return prepare_hybrid_dataset(
            task_file=task_file,
            output_dir=dataset_dir,
            split=split,
            headed=headed,
            pilot_cohort=pilot_cohort,
        )
    if dataset_dir is None:
        raise ValueError(f"hybrid visual phase {phase} requires --dataset-dir")
    if phase == "judge":
        return judge_hybrid_dataset(
            dataset_dir=dataset_dir,
            p5_manifest=p5_manifest,
            credentials_file=credentials_file,
        )
    if phase == "annotate":
        if annotator_id is None:
            raise ValueError("annotate requires --annotator")
        if annotation_mode == "vlm":
            return annotate_hybrid_dataset_with_vlm(
                dataset_dir=dataset_dir,
                annotator_id=annotator_id,
                model_spec=annotator_model,
                p5_manifest=p5_manifest,
                credentials_file=credentials_file,
            )
        if annotation_mode != "human":
            raise ValueError(f"Unsupported annotation mode: {annotation_mode}")
        if annotator_model is not None:
            raise ValueError("--annotator-model requires --annotation-mode vlm")
        return run_annotation_server(
            dataset_dir=dataset_dir,
            annotator_id=annotator_id,
            adjudicate=False,
        )
    if phase == "adjudicate":
        if annotation_mode == "vlm":
            return annotate_hybrid_dataset_with_vlm(
                dataset_dir=dataset_dir,
                annotator_id=annotator_id or ADJUDICATOR_ID,
                model_spec=annotator_model,
                adjudicate=True,
                p5_manifest=p5_manifest,
                credentials_file=credentials_file,
            )
        if annotation_mode != "human":
            raise ValueError(f"Unsupported annotation mode: {annotation_mode}")
        if annotator_model is not None:
            raise ValueError("--annotator-model requires --annotation-mode vlm")
        return run_annotation_server(
            dataset_dir=dataset_dir,
            annotator_id=annotator_id or ADJUDICATOR_ID,
            adjudicate=True,
        )
    if phase == "aggregate":
        return aggregate_hybrid_dataset(dataset_dir=dataset_dir)
    raise ValueError(f"Unsupported hybrid visual phase: {phase}")


__all__ = [
    "ADJUDICATOR_ID",
    "ANNOTATOR_IDS",
    "BROWSER_OVERLAY_BENCHMARKS",
    "DEFAULT_VLM_ANNOTATOR_MODELS",
    "EXPECTED_BENCHMARKS",
    "FORMAL_VARIANTS",
    "HybridAnnotationSession",
    "HybridTask",
    "PROTOCOL",
    "aggregate_hybrid_dataset",
    "annotate_hybrid_dataset_with_vlm",
    "annotation_order",
    "build_samples_from_bases",
    "choose_wrong_state_donor",
    "corrupt_image",
    "judge_hybrid_dataset",
    "load_c1_tasks",
    "load_hybrid_manifest",
    "normalized_pixel_distance",
    "prepare_hybrid_dataset",
    "run_hybrid_visual_phase",
    "select_formal_tasks",
    "select_pilot_tasks",
]
