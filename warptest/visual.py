from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import io
import json
import multiprocessing as mp
import os
import queue as queue_module
import re
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from . import cost, run_context
from .c1_evaluation import semantic_assertions, visual_oracle as spec_visual_oracle
from .c1_task_deadline import (
    bounded_task_timeout_s,
    current_task_deadline,
    raise_task_timeout_if_expired,
)
from .models import PhaseResult, RunReport
from .model_request import resolve_model_request_profile
from .runtime import BrowserRuntimeAdapter


VISUAL_LABELS = {"correct", "incorrect", "uncertain"}
DEFAULT_VISUAL_JUDGE_BASE_URL = "https://api-inference.modelscope.cn/v1"
DEFAULT_VISUAL_JUDGE_MODEL = "Qwen/Qwen3.5-397B-A17B"
VISUAL_JUDGE_MAX_IMAGE_SIDE = 2048
VISUAL_JUDGE_MAX_IMAGE_BYTES = 512 * 1024
VISUAL_AUDIT_PROMPT_VERSION = 2
VISUAL_AUDIT_CACHE_VERSION = 1
NORMALIZED_IMAGE_EQUALITY_VERSION = 1
CASCADE_POLICY_CHOICES = ("full-only", "conservative")
VISUAL_CACHE_CHOICES = ("off", "report", "global", "report+global")
DETERMINISTIC_VISUAL_ONLY_ENV = "WARPTEST_DETERMINISTIC_VISUAL_ONLY"
VISUAL_JUDGE_OMIT_TEMPERATURE_MODELS = frozenset({"claude-sonnet-5"})


def _visual_judge_request_parameters(
    model: str,
    messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the frozen OpenAI-compatible request shape for a visual judge.

    Relay rejects an explicit ``temperature`` for ``claude-sonnet-5``.  The
    omission is model-registered up front so a paid adjudication does not need
    an avoidable rejected probe before its one valid request.
    """

    parameters: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    if model.split("@", 1)[0] not in VISUAL_JUDGE_OMIT_TEMPERATURE_MODELS:
        parameters["temperature"] = 0
    return parameters


def deterministic_visual_only() -> bool:
    """True when the visual channel must never escalate to the VLM judge.

    Used by the replay-determinism experiment, whose visual verdict is
    normalized image identity rather than rubric satisfaction.
    """

    return os.environ.get(DETERMINISTIC_VISUAL_ONLY_ENV) == "1"


@contextmanager
def _wall_clock_timeout(seconds: float, label: str):
    if seconds <= 0 or threading.current_thread() is not threading.main_thread():
        yield
        return

    def _raise_timeout(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"{label} timed out after {seconds:.0f}s")

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


@dataclass(frozen=True)
class CascadePolicyConfig:
    cascade_policy: str = "full-only"
    sample_count: int | None = None
    sample_interval_ms: int | None = None
    cache_mode: str = "report"


def cascade_policy_config(
    *,
    cascade_policy: str | CascadePolicyConfig | None = None,
    sample_count: int | None = None,
    sample_interval_ms: int | None = None,
    visual_cache: str | None = None,
) -> CascadePolicyConfig:
    if isinstance(cascade_policy, CascadePolicyConfig):
        return cascade_policy
    policy = cascade_policy or os.environ.get("WARPTEST_VISUAL_CASCADE_POLICY") or "full-only"
    if policy not in CASCADE_POLICY_CHOICES:
        raise ValueError(f"Unsupported cascade policy: {policy}")
    resolved_sample_count = _optional_int(sample_count, "sample_count")
    if resolved_sample_count is None:
        resolved_sample_count = _optional_env_int("WARPTEST_VISUAL_FRAME_SAMPLE_COUNT")
    if resolved_sample_count is not None and resolved_sample_count < 1:
        raise ValueError("sample_count must be >= 1")
    resolved_interval = _optional_int(sample_interval_ms, "sample_interval_ms")
    if resolved_interval is None:
        resolved_interval = _optional_env_int("WARPTEST_VISUAL_FRAME_SAMPLE_INTERVAL_MS")
    if resolved_interval is not None and resolved_interval < 0:
        raise ValueError("sample_interval_ms must be >= 0")
    cache_mode = visual_cache or os.environ.get("WARPTEST_VISUAL_CACHE") or "report"
    if cache_mode not in VISUAL_CACHE_CHOICES:
        raise ValueError(f"Unsupported visual cache mode: {cache_mode}")
    return CascadePolicyConfig(
        cascade_policy=policy,
        sample_count=resolved_sample_count,
        sample_interval_ms=resolved_interval,
        cache_mode=cache_mode,
    )


class VisualOracleError(ValueError):
    pass


def validate_visual_oracle(spec: dict[str, Any], spec_path: str | Path, required: bool = False) -> dict[str, Any] | None:
    oracle = spec_visual_oracle(spec)
    if oracle is None:
        if required:
            raise VisualOracleError("Spec is missing required visual_oracle.")
        return None
    if not isinstance(oracle, dict):
        raise VisualOracleError("visual_oracle must be a mapping.")

    expected_label = oracle.get("expected_label")
    if expected_label not in VISUAL_LABELS:
        raise VisualOracleError("visual_oracle.expected_label must be correct, incorrect, or uncertain.")

    description = oracle.get("description")
    if not isinstance(description, str) or not description.strip():
        raise VisualOracleError("visual_oracle.description must be a non-empty string.")

    golden_value = oracle.get("golden_image")
    if not isinstance(golden_value, str) or not golden_value.strip():
        raise VisualOracleError("visual_oracle.golden_image must be a non-empty path string.")
    golden_path = Path(golden_value).expanduser()
    if not golden_path.is_absolute():
        golden_path = Path(spec_path).expanduser().resolve().parent / golden_path
    golden_path = golden_path.resolve()
    if not golden_path.exists():
        raise VisualOracleError(f"visual_oracle.golden_image does not exist: {golden_path}")

    rubric = oracle.get("rubric")
    if not isinstance(rubric, dict):
        raise VisualOracleError("visual_oracle.rubric must be a mapping.")
    normalized_rubric: dict[str, list[str]] = {}
    for key in ("must", "must_not", "ignore"):
        values = rubric.get(key, [])
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            raise VisualOracleError(f"visual_oracle.rubric.{key} must be a list of strings.")
        normalized_rubric[key] = values

    capture = oracle.get("capture", {})
    if capture is None:
        capture = {}
    if not isinstance(capture, dict):
        raise VisualOracleError("visual_oracle.capture must be a mapping.")
    stage = capture.get("stage", "post_smoke")
    if stage not in {"post_smoke", "pre_smoke"}:
        raise VisualOracleError("visual_oracle.capture.stage must be post_smoke or pre_smoke.")
    wait_ms = int(capture.get("wait_ms", 300))
    if wait_ms < 0:
        raise VisualOracleError("visual_oracle.capture.wait_ms must be >= 0.")
    sample_count = capture.get("sample_count")
    if sample_count is not None:
        sample_count = int(sample_count)
        if sample_count < 1:
            raise VisualOracleError("visual_oracle.capture.sample_count must be >= 1.")
    sample_interval_ms = capture.get("sample_interval_ms")
    if sample_interval_ms is not None:
        sample_interval_ms = int(sample_interval_ms)
        if sample_interval_ms < 0:
            raise VisualOracleError("visual_oracle.capture.sample_interval_ms must be >= 0.")
    # Optional list of CSS selectors for transient overlays (story-briefing dialogs,
    # random-event modals, etc.) to hide right before capture. Hiding is done with an
    # injected stylesheet (display:none), so it only affects the rendered frame and
    # never mutates game state. Applied identically to golden regeneration and every
    # live run, so it keeps the visual comparison fair while removing overlays that
    # would otherwise obscure the target UI and confuse weaker visual judges.
    hide_selectors = capture.get("hide_selectors", [])
    if hide_selectors is None:
        hide_selectors = []
    if not isinstance(hide_selectors, list) or any(not isinstance(item, str) for item in hide_selectors):
        raise VisualOracleError("visual_oracle.capture.hide_selectors must be a list of strings.")

    normalized = {
        "golden_image": str(golden_path),
        "golden_image_declared": golden_value,
        "expected_label": expected_label,
        "description": description.strip(),
        "rubric": normalized_rubric,
        "capture": {
            "stage": stage,
            "wait_ms": wait_ms,
            "full_page": bool(capture.get("full_page", False)),
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "hide_selectors": list(hide_selectors),
        },
    }
    scene_key = oracle.get("scene_key")
    if scene_key is not None:
        if not isinstance(scene_key, str) or not scene_key.strip():
            raise VisualOracleError("visual_oracle.scene_key must be a non-empty string when present.")
        normalized["scene_key"] = scene_key.strip()
    # Optional before/after golden pair for the state-loop benchmark. When present,
    # `golden_image` is treated as the "after" golden by single-image consumers; the
    # pair audit (`run_visual_pair_audit`) uses both. Absent keys fall back to the
    # single golden so existing specs keep working unchanged.
    for pair_key in ("golden_before", "golden_after"):
        value = oracle.get(pair_key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise VisualOracleError(f"visual_oracle.{pair_key} must be a non-empty path string when present.")
        pair_path = Path(value).expanduser()
        if not pair_path.is_absolute():
            pair_path = Path(spec_path).expanduser().resolve().parent / pair_path
        pair_path = pair_path.resolve()
        if not pair_path.exists():
            raise VisualOracleError(f"visual_oracle.{pair_key} does not exist: {pair_path}")
        normalized[pair_key] = str(pair_path)
    for desc_key in ("before_description", "after_description"):
        value = oracle.get(desc_key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise VisualOracleError(f"visual_oracle.{desc_key} must be a non-empty string when present.")
        normalized[desc_key] = value.strip()
    for rubric_key in ("before_rubric", "after_rubric"):
        value = oracle.get(rubric_key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise VisualOracleError(f"visual_oracle.{rubric_key} must be a mapping when present.")
        normalized_rubric = {}
        for key in ("must", "must_not", "ignore"):
            values = value.get(key, [])
            if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
                raise VisualOracleError(f"visual_oracle.{rubric_key}.{key} must be a list of strings.")
            normalized_rubric[key] = values
        normalized[rubric_key] = normalized_rubric
    return normalized


def build_pair_judge_prompt(
    *,
    stage: str,
    description: str,
    rubric: dict[str, list[str]] | None = None,
    expected_label: str = "correct",
) -> str:
    """Prompt for judging one half (before/after) of a state-loop screenshot pair.

    The judge receives the oracle golden screenshot for this stage first, then the
    method's live screenshot. It decides whether the live frame matches the golden
    deeply enough to count as the same semantic state.
    """

    payload = {
        "task": (
            f"Judge whether the live '{stage}' screenshot from a candidate code/config change "
            f"reaches the same GUI state as the oracle '{stage}' golden screenshot."
        ),
        "stage": stage,
        "output_contract": {
            "label": "correct|incorrect|uncertain",
            "confidence": "number from 0.0 to 1.0",
            "observed": "non-empty short visual summary of the live screenshot",
            "rationale": "non-empty short reason grounded in the two screenshots",
            "rubric_failures": (
                "non-empty list of unmet must/must_not items when label=incorrect; "
                "empty list when label=correct"
            ),
        },
        "instructions": [
            "You receive exactly two images: first the oracle golden screenshot, then the live screenshot to judge.",
            "Use the golden screenshot as the binding visual reference for the required GUI/game state, HUD, board, and coarse layout.",
            "Label correct only when the live screenshot shows the same semantic state as the golden for this stage.",
            "Label incorrect for a wrong screen, missing required state, blank canvas, crash, or visible error UI.",
            "When label=incorrect, rubric_failures must name at least one concrete visible failure. When label=correct, rubric_failures must be empty.",
            "observed and rationale must both be non-empty and must support the returned label; do not put a revised or contradictory final label inside either field.",
            "Label uncertain only when the screenshots are too ambiguous to decide.",
            "Base observations only on visible pixels; do not invent unseen dialogs, logs, or screens.",
            "Return strict JSON only. Do not wrap it in Markdown.",
        ],
        "oracle": {
            "expected_label": expected_label,
            "description": description,
            "rubric": rubric or {"must": [], "must_not": [], "ignore": []},
        },
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _pair_cache_key(*, stage: str, prompt: str, golden: Path, live: Path, judge_backend: str) -> str:
    payload = {
        "cache_version": VISUAL_AUDIT_CACHE_VERSION,
        "prompt_version": VISUAL_AUDIT_PROMPT_VERSION,
        "kind": "state_loop_pair",
        "stage": stage,
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "golden_hash": hashlib.sha256(golden.read_bytes()).hexdigest(),
        "live_hash": hashlib.sha256(live.read_bytes()).hexdigest(),
        "judge_backend": judge_backend,
        "full_model": _visual_model_id(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _normalized_image_identity(path: Path) -> dict[str, Any]:
    """Return the canonical visual identity used by the exact-match grader.

    File-container bytes are deliberately not compared: two lossless images can
    render identically while carrying different PNG compression or metadata.  We
    apply EXIF orientation, convert to one RGBA layout, and hash the dimensions
    plus decoded pixels.  This remains an exact test -- one differing normalized
    pixel yields a different digest and falls through to the visual judge.
    """

    with Image.open(path) as source:
        normalized = ImageOps.exif_transpose(source).convert("RGBA")
        normalized.load()
        width, height = normalized.size
        pixel_bytes = normalized.tobytes()
    header = json.dumps(
        {
            "version": NORMALIZED_IMAGE_EQUALITY_VERSION,
            "mode": "RGBA",
            "width": width,
            "height": height,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256(header + b"\0" + pixel_bytes).hexdigest()
    return {
        "normalization": "exif_transpose_rgba_pixels_v1",
        "normalized_sha256": digest,
        "raw_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "width": width,
        "height": height,
        "mode": "RGBA",
    }


def _normalized_image_equality(live_image: Path, golden_image: Path) -> dict[str, Any]:
    live = _normalized_image_identity(live_image)
    golden = _normalized_image_identity(golden_image)
    return {
        "protocol": "normalized_image_equality_v1",
        "match": (
            live["width"] == golden["width"]
            and live["height"] == golden["height"]
            and live["normalized_sha256"] == golden["normalized_sha256"]
        ),
        "live": live,
        "golden": golden,
    }


def _judge_pair_half(
    *,
    stage: str,
    live_image: Path,
    golden_image: Path,
    description: str,
    rubric: dict[str, list[str]] | None,
    expected_label: str,
    cache_mode: str,
) -> dict[str, Any]:
    """Judge one before/after half, reusing the visual judge client and global cache."""

    quality = screenshot_quality(live_image)
    if not quality["informative"]:
        return {
            "stage": stage,
            "label": "incorrect",
            "confidence": 1.0,
            "observed": "Live screenshot is blank or non-informative.",
            "rationale": "Capture produced an empty or near-empty frame.",
            "judge_model": None,
            "verdict_source": "non_informative_frame_rejection_v1",
            "rubric_failures": ["live screenshot is non-informative"],
            "expected_label": expected_label,
            "live_screenshot": str(live_image),
            "golden_image": str(golden_image),
            "quality": quality,
            "cache_hit": False,
        }
    try:
        deterministic_equality = _normalized_image_equality(
            live_image, golden_image
        )
    except Exception as error:
        deterministic_equality = {
            "protocol": "normalized_image_equality_v1",
            "match": False,
            "error": run_context.safe_error_text(str(error), limit=1000),
        }
    if deterministic_equality["match"]:
        normalized_sha256 = deterministic_equality["live"][
            "normalized_sha256"
        ]
        return {
            "stage": stage,
            "label": "correct",
            "confidence": 1.0,
            "observed": (
                "The live screenshot is pixel-identical to the registered "
                "golden after canonical image normalization."
            ),
            "rationale": (
                "Deterministic normalized image equality is sufficient for the "
                "required visual state; no LLM visual judgment was requested."
            ),
            "rubric_failures": [],
            "expected_label": expected_label,
            "judge_model": None,
            "verdict_source": "normalized_image_equality_v1",
            "live_screenshot": str(live_image),
            "golden_image": str(golden_image),
            "quality": quality,
            "cache_hit": False,
            "cache_policy": "not_applicable_deterministic_equality",
            "normalized_image_equality": deterministic_equality,
            "normalized_image_sha256": normalized_sha256,
        }
    if deterministic_visual_only():
        # Replay determinism (C4) grades rendering identity, not rubric
        # satisfaction, and must stay free of provider calls even when the
        # frame drifts. Escalating here would both spend tokens and silently
        # convert a rendering-drift observation into a rubric judgement.
        return {
            "stage": stage,
            "label": "incorrect",
            "confidence": 1.0,
            "observed": (
                "The live screenshot is not pixel-identical to the registered "
                "golden after canonical image normalization."
            ),
            "rationale": (
                "Deterministic-visual-only mode was requested, so the visual "
                "judge was not consulted; this records rendering drift rather "
                "than a rubric verdict."
            ),
            "rubric_failures": ["normalized image equality mismatch"],
            "expected_label": expected_label,
            "judge_model": None,
            "verdict_source": "normalized_image_equality_v1",
            "live_screenshot": str(live_image),
            "golden_image": str(golden_image),
            "quality": quality,
            "cache_hit": False,
            "cache_policy": "not_applicable_deterministic_equality",
            "normalized_image_equality": deterministic_equality,
            "deterministic_visual_only": True,
        }
    judge_backend = _judge_backend()
    prompt = build_pair_judge_prompt(
        stage=stage, description=description, rubric=rubric, expected_label=expected_label
    )
    cache_key = _pair_cache_key(
        stage=stage, prompt=prompt, golden=golden_image, live=live_image, judge_backend=judge_backend
    )
    c1_cache_disabled = os.environ.get("WARPTEST_C1_DISABLE_VISUAL_CACHE") == "1"
    use_global_cache = (
        not c1_cache_disabled
        and judge_backend != "fake"
        and cache_mode in {"global", "report+global"}
    )
    cached = _read_global_cache(cache_key) if use_global_cache else None
    if cached is not None:
        verdict = cached["final_verdict"]
        cache_hit = True
    else:
        try:
            verdict = VisualJudgeClient.from_env().judge(
                prompt=prompt, live_screenshot=live_image, golden_image=golden_image
            )
        except Exception as error:
            from .lazarus_p2p_only_recovery import classify_failure
            return {
                "stage": stage,
                "label": "uncertain",
                "judge_failure_details": classify_failure(error),
                "confidence": 0.0,
                "observed": "Visual judge did not return a usable verdict.",
                "rationale": (
                    "Visual judge failed closed: "
                    + run_context.safe_error_text(str(error), limit=1000)
                ),
                "rubric_failures": ["visual judge failed"],
                "expected_label": expected_label,
                "live_screenshot": str(live_image),
                "golden_image": str(golden_image),
                "quality": quality,
                "cache_hit": False,
                "judge_failure": True,
                "judge_error": run_context.safe_error_text(str(error), limit=1000),
                "verdict_source": "llm_visual_judge",
                "normalized_image_equality": deterministic_equality,
            }
        cache_hit = False
        if use_global_cache:
            _write_global_cache(
                cache_key,
                {"cache_version": VISUAL_AUDIT_CACHE_VERSION, "final_verdict": verdict},
            )
    return {
        "stage": stage,
        "label": verdict["label"],
        "confidence": verdict.get("confidence"),
        "observed": verdict.get("observed", ""),
        "rationale": verdict.get("rationale", ""),
        "rubric_failures": verdict.get("rubric_failures", []),
        "expected_label": expected_label,
        "judge_model": verdict.get("judge_model"),
        "live_screenshot": str(live_image),
        "golden_image": str(golden_image),
        "quality": quality,
        "cache_hit": cache_hit,
        "cache_policy": "disabled_for_c1" if c1_cache_disabled else cache_mode,
        "verdict_source": "llm_visual_judge",
        "normalized_image_equality": deterministic_equality,
    }


def run_visual_pair_audit(
    *,
    live_before: str | Path,
    live_after: str | Path | None,
    golden_before: str | Path,
    golden_after: str | Path,
    description: str,
    rubric: dict[str, list[str]] | None = None,
    before_description: str | None = None,
    after_description: str | None = None,
    before_rubric: dict[str, list[str]] | None = None,
    after_rubric: dict[str, list[str]] | None = None,
    cache_mode: str = "report+global",
    evaluate_after: bool = True,
    prior_before: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Judge a before/after screenshot pair against oracle goldens.

    The state-loop task passes if BOTH halves are judged ``correct``: the candidate
    config must reach the same start state as the oracle AND drive the same visible
    transition after the smoke actions. A definitive before failure short-circuits
    the after request. GUI callers can set ``evaluate_after=False`` before Phase B,
    then pass the returned ``before`` mapping as ``prior_before`` after Phase B so
    the paid before judgment is neither repeated nor allowed to drift. Each evaluated
    half reuses the existing visual judge client (honouring
    ``WARPTEST_VISUAL_JUDGE_FAKE``) and the global verdict cache.

    The before half is deterministic (a restored start state), so it is judged
    against the golden with a generic "same state" prompt. The after half can be
    nondeterministic (e.g. 2048 spawns a random tile per move), so it accepts an
    oracle-authored ``after_rubric``/``after_description`` that tells the judge which
    differences to ignore. ``description``/``rubric`` are defaults for both halves.
    """

    if prior_before is None:
        before = _judge_pair_half(
            stage="before",
            live_image=Path(live_before),
            golden_image=Path(golden_before),
            description=before_description or description,
            rubric=before_rubric if before_rubric is not None else rubric,
            expected_label="correct",
            cache_mode=cache_mode,
        )
    else:
        before = dict(prior_before)

    before_ok = before.get("label") == "correct"
    if not evaluate_after or not before_ok:
        reason = (
            "pending_phase_b"
            if before_ok
            else "not_evaluated_before_failed"
        )
        after = {
            "stage": "after",
            "label": "not_evaluated",
            "evaluation_status": reason,
        }
        return {
            "result": False,
            "status": "pending" if before_ok else "failure",
            "before": before,
            "after": after,
            "after_evaluated": False,
            "judge_backend": _judge_backend(),
        }

    if live_after is None:
        raise ValueError("live_after is required when evaluate_after=True")
    after = _judge_pair_half(
        stage="after",
        live_image=Path(live_after),
        golden_image=Path(golden_after),
        description=after_description or description,
        rubric=after_rubric if after_rubric is not None else rubric,
        expected_label="correct",
        cache_mode=cache_mode,
    )
    passed = before["label"] == "correct" and after["label"] == "correct"
    return {
        "result": passed,
        "status": "success" if passed else "failure",
        "before": before,
        "after": after,
        "after_evaluated": True,
        "judge_backend": _judge_backend(),
    }


def run_visual_oracle(
    *,
    spec: dict[str, Any],
    spec_path: str | Path,
    runtime: BrowserRuntimeAdapter,
    report: RunReport,
    required: bool = False,
    capture_stage: str | None = None,
    cascade_config: CascadePolicyConfig | None = None,
) -> PhaseResult:
    result = run_visual_audit(
        spec=spec,
        spec_path=spec_path,
        runtime=runtime,
        report=report,
        required=required,
        capture_stage=capture_stage,
        cascade_config=cascade_config,
        oracle_policy="visual",
        visual_audit_reason="legacy_visual_oracle",
        visual_sample_rate=0.0,
    )
    _apply_visual_status(report)
    return result


def run_visual_audit(
    *,
    spec: dict[str, Any],
    spec_path: str | Path,
    runtime: BrowserRuntimeAdapter,
    report: RunReport,
    required: bool = True,
    capture_stage: str | None = None,
    cascade_config: CascadePolicyConfig | None = None,
    oracle_policy: str = "visual",
    visual_audit_reason: str = "forced",
    visual_sample_rate: float = 0.0,
) -> PhaseResult:
    cascade = cascade_config or CascadePolicyConfig()
    audit: dict[str, Any] = {
        "cascade_policy": cascade.cascade_policy,
        "judge_backend": _judge_backend(),
        "sample_count": None,
        "sample_interval_ms": None,
        "visual_sample_rate": visual_sample_rate,
        "cache_hit": False,
        "global_cache_written": False,
        "frame_paths": [],
        "contact_sheet_path": None,
        "full_verdict": None,
        "final_verdict_source": None,
        "audit_decision_reason": visual_audit_reason,
    }
    try:
        oracle = validate_visual_oracle(spec, spec_path, required=required)
    except Exception as error:
        result = PhaseResult(
            "failure",
            str(error),
            {"oracle_policy": oracle_policy, "visual_audit_reason": visual_audit_reason, "visual_audit": audit},
        )
        report.visual_oracle_result = result
        return result

    if oracle is None:
        result = PhaseResult(
            "skipped",
            "Spec does not declare visual_oracle.",
            {"oracle_policy": oracle_policy, "visual_audit_reason": visual_audit_reason, "visual_audit": audit},
        )
        report.visual_oracle_result = result
        return result

    capture = dict(oracle["capture"])
    if capture_stage is not None:
        capture["stage"] = capture_stage
    sample_count = _resolved_sample_count(cascade, capture, report)
    sample_interval_ms = _resolved_sample_interval_ms(cascade, capture, sample_count)
    audit["sample_count"] = sample_count
    audit["sample_interval_ms"] = sample_interval_ms
    capture_metadata = _runtime_capture_metadata(runtime)
    screenshots = capture_metadata.get("screenshots") if isinstance(capture_metadata.get("screenshots"), dict) else {}
    live_before = capture_metadata.get("screenshot_before") or screenshots.get("before")
    live_after = capture_metadata.get("screenshot_after") or screenshots.get("after")
    if oracle.get("golden_before") and oracle.get("golden_after") and live_before and live_after:
        pair_paths = [str(live_before), str(live_after)]
        for frame_path in pair_paths:
            if frame_path not in report.screenshots:
                report.screenshots.append(frame_path)
        audit["live_source"] = capture_metadata.get("live_source", "runtime_pair_capture")
        audit["capture_metadata"] = capture_metadata
        audit["frame_paths"] = pair_paths
        try:
            pair_verdict = run_visual_pair_audit(
                live_before=live_before,
                live_after=live_after,
                golden_before=oracle["golden_before"],
                golden_after=oracle["golden_after"],
                description=oracle["description"],
                rubric=oracle["rubric"],
                before_description=oracle.get("before_description"),
                after_description=oracle.get("after_description"),
                before_rubric=oracle.get("before_rubric"),
                after_rubric=oracle.get("after_rubric"),
                cache_mode=cascade.cache_mode,
            )
        except Exception as error:
            result = PhaseResult(
                "failure",
                "Visual pair audit failed before visual scoring completed.",
                {
                    "live_source": audit["live_source"],
                    "capture_failure": True,
                    "capture_error": str(error),
                    "golden_before": oracle["golden_before"],
                    "golden_after": oracle["golden_after"],
                    "oracle_policy": oracle_policy,
                    "visual_audit_reason": visual_audit_reason,
                    "visual_audit": audit,
                },
            )
            report.visual_oracle_result = result
            return result
        status = "success" if pair_verdict.get("status") == "success" else "failure"
        result = PhaseResult(
            status,
            f"Visual judge labeled LibreOffice before/after pair as {pair_verdict.get('status')}.",
            {
                "pair_verdict": pair_verdict,
                "live_source": audit["live_source"],
                "capture_metadata": capture_metadata,
                "live_before": str(live_before),
                "live_after": str(live_after),
                "golden_before": oracle["golden_before"],
                "golden_after": oracle["golden_after"],
                "oracle_policy": oracle_policy,
                "visual_audit_reason": visual_audit_reason,
                "visual_audit": audit,
            },
        )
        report.visual_oracle_result = result
        return result
    if capture["wait_ms"]:
        _runtime_wait(runtime, capture["wait_ms"])
    _hide_capture_overlays(runtime, capture.get("hide_selectors") or [])
    try:
        page = getattr(runtime, "page", None)
        if page is not None:
            page.evaluate("() => document.fonts?.ready")
    except Exception:
        pass

    try:
        frame_paths = _capture_visual_frames(runtime, capture, sample_count=sample_count, sample_interval_ms=sample_interval_ms)
    except Exception as error:
        capture_metadata = _runtime_capture_metadata(runtime)
        audit["live_source"] = capture_metadata.get("live_source", "capture_failure")
        audit["capture_metadata"] = capture_metadata
        result = PhaseResult(
            "failure",
            "Visual capture failed before visual scoring.",
            {
                "live_source": audit["live_source"],
                "capture_failure": True,
                "capture_error": str(error),
                "golden_image": oracle["golden_image"],
                "oracle_policy": oracle_policy,
                "visual_audit_reason": visual_audit_reason,
                "visual_audit": audit,
            },
        )
        report.visual_oracle_result = result
        return result

    capture_metadata = _runtime_capture_metadata(runtime)
    audit["live_source"] = capture_metadata.get("live_source", "runtime_capture")
    audit["capture_metadata"] = capture_metadata
    audit["frame_paths"] = frame_paths
    for frame_path in frame_paths:
        if frame_path not in report.screenshots:
            report.screenshots.append(frame_path)

    frame_quality = {path: screenshot_quality(Path(path)) for path in frame_paths}
    informative_frames = [path for path in frame_paths if frame_quality[path]["informative"]]
    if _requires_unity_capture(report) and audit["live_source"] != "unity_capture":
        result = PhaseResult(
            "failure",
            "Visual capture failed before visual scoring.",
            {
                "live_source": audit["live_source"],
                "capture_failure": True,
                "diagnostic_screenshot": frame_paths[0] if frame_paths else None,
                "golden_image": oracle["golden_image"],
                "quality": frame_quality,
                "oracle_policy": oracle_policy,
                "visual_audit_reason": visual_audit_reason,
                "visual_audit": audit,
            },
        )
        report.visual_oracle_result = result
        return result

    if not informative_frames:
        result = PhaseResult(
            "failure",
            "Visual screenshot is blank or non-informative.",
            {
                "live_screenshot": frame_paths[0] if frame_paths else None,
                "live_source": audit["live_source"],
                "golden_image": oracle["golden_image"],
                "quality": frame_quality,
                "oracle_policy": oracle_policy,
                "visual_audit_reason": visual_audit_reason,
                "visual_audit": audit,
            },
        )
        report.visual_oracle_result = result
        return result

    unique_frames = _unique_frame_paths(informative_frames)
    if len(unique_frames) > 1:
        contact_sheet_path = runtime.report_dir / f"visual-{capture['stage']}-contact-sheet.png"
        _write_contact_sheet([Path(path) for path in unique_frames], contact_sheet_path)
        live_judge_path = contact_sheet_path
        audit["contact_sheet_path"] = str(contact_sheet_path)
        if str(contact_sheet_path) not in report.screenshots:
            report.screenshots.append(str(contact_sheet_path))
    else:
        live_judge_path = Path(unique_frames[0])

    prompt_text = build_visual_prompt(
        spec=spec,
        oracle=oracle,
        report=report,
        visual_audit={"sample_count": sample_count, "frame_count": len(unique_frames), "live_image_kind": "contact_sheet" if audit["contact_sheet_path"] else "single_frame"},
    )
    prompt_path = runtime.report_dir / "visual-judge-prompt.json"
    raw_response_path = runtime.report_dir / "visual-judge-response.json"
    max_image_side = visual_judge_max_image_side()
    prompt_artifact = {
        "prompt": prompt_text,
        "live_screenshot": str(live_judge_path),
        "live_source": audit["live_source"],
        "frame_paths": frame_paths,
        "contact_sheet_path": audit["contact_sheet_path"],
        "golden_image": oracle["golden_image"],
        "judge_images": {
            "max_side": max_image_side,
            "live_screenshot": visual_judge_image_metadata(live_judge_path, max_side=max_image_side),
            "golden_image": visual_judge_image_metadata(Path(oracle["golden_image"]), max_side=max_image_side),
        },
        "spec_path": str(Path(spec_path).resolve()),
        "oracle": oracle,
        "visual_audit": audit,
    }
    prompt_path.write_text(json.dumps(prompt_artifact, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    cache_key = _visual_audit_cache_key(
        prompt=prompt_text,
        oracle=oracle,
        golden_image=Path(oracle["golden_image"]),
        frame_paths=[Path(path) for path in unique_frames],
        cascade=cascade,
        judge_backend=audit["judge_backend"],
    )
    if _can_use_global_cache(cascade, audit["judge_backend"]):
        cached = _read_global_cache(cache_key)
        if cached is not None:
            audit["cache_hit"] = True
            audit["final_verdict_source"] = "cache"
            final_verdict = cached["final_verdict"]
            payload = _visual_result_payload(
                final_verdict,
                oracle=oracle,
                live_judge_path=live_judge_path,
                prompt_path=prompt_path,
                raw_response_path=raw_response_path,
                quality=screenshot_quality(live_judge_path),
                audit=audit,
            )
            raw_response_path.write_text(json.dumps({**final_verdict, "visual_audit": audit}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            status = "success" if final_verdict["label"] == oracle["expected_label"] else "failure"
            result = PhaseResult(status, f"Visual judge labeled screenshot as {final_verdict['label']}.", payload)
            report.visual_oracle_result = result
            return result

    try:
        final_verdict = _run_visual_judge(
            prompt=prompt_text,
            live_judge_path=live_judge_path,
            golden_image=Path(oracle["golden_image"]),
            audit=audit,
        )
    except Exception as error:
        safe_error = run_context.safe_error_text(str(error), limit=1000)
        raw_response_path.write_text(json.dumps({"error": safe_error}, indent=2) + "\n", encoding="utf-8")
        result = PhaseResult(
            "failure",
            f"Visual judge failed: {safe_error}",
            {
                "live_screenshot": str(live_judge_path),
                "live_source": audit.get("live_source", "runtime_capture"),
                "golden_image": oracle["golden_image"],
                "prompt_path": str(prompt_path),
                "raw_response_path": str(raw_response_path),
                "quality": screenshot_quality(live_judge_path),
                "oracle_policy": oracle_policy,
                "visual_audit_reason": visual_audit_reason,
                "visual_audit": audit,
            },
        )
        report.visual_oracle_result = result
        return result

    if _can_use_global_cache(cascade, audit["judge_backend"]):
        audit["global_cache_written"] = _write_global_cache(
            cache_key,
            {
                "cache_version": VISUAL_AUDIT_CACHE_VERSION,
                "final_verdict": final_verdict,
                "visual_audit": audit,
            },
        )
    raw_response_path.write_text(json.dumps({**final_verdict, "visual_audit": audit}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    status = "success" if final_verdict["label"] == oracle["expected_label"] else "failure"
    result = PhaseResult(
        status,
        f"Visual judge labeled screenshot as {final_verdict['label']}.",
        _visual_result_payload(
            final_verdict,
            oracle=oracle,
            live_judge_path=live_judge_path,
            prompt_path=prompt_path,
            raw_response_path=raw_response_path,
            quality=screenshot_quality(live_judge_path),
            audit=audit,
        ),
    )
    report.visual_oracle_result = result
    return result


def _apply_visual_status(report: RunReport) -> None:
    if report.visual_oracle_result.status == "failure":
        report.overall_status = "failure"


def _runtime_capture_metadata(runtime: BrowserRuntimeAdapter) -> dict[str, Any]:
    metadata = getattr(runtime, "last_screenshot_metadata", None)
    if callable(metadata):
        metadata = metadata()
    if not isinstance(metadata, dict):
        return {"live_source": "runtime_capture"}
    live_source = metadata.get("live_source") or metadata.get("screenshot_source") or "runtime_capture"
    return {**metadata, "live_source": live_source}


def _requires_unity_capture(report: RunReport) -> bool:
    return report.benchmark == "jynew" or report.profile == "JynewProfile"


def _optional_int(value: int | str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be an integer") from error


def _optional_env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value in {None, ""}:
        return None
    return _optional_int(value, name)


def _judge_backend() -> str:
    return "fake" if os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE") else "full"


def _resolved_sample_count(cascade: CascadePolicyConfig, capture: dict[str, Any], report: RunReport) -> int:
    if cascade.sample_count is not None:
        return cascade.sample_count
    if capture.get("sample_count") is not None:
        return int(capture["sample_count"])
    if cascade.cascade_policy == "conservative" and not _heavy_visual_runtime(report):
        return 3
    return 1


def _resolved_sample_interval_ms(cascade: CascadePolicyConfig, capture: dict[str, Any], sample_count: int) -> int:
    if cascade.sample_interval_ms is not None:
        return cascade.sample_interval_ms
    if capture.get("sample_interval_ms") is not None:
        return int(capture["sample_interval_ms"])
    return 300 if sample_count > 1 else 0


def _heavy_visual_runtime(report: RunReport) -> bool:
    return report.benchmark in {"OpenRA", "jynew"} or report.profile in {"OpenRAProfile", "JynewProfile"}


def _capture_visual_frames(
    runtime: BrowserRuntimeAdapter,
    capture: dict[str, Any],
    *,
    sample_count: int,
    sample_interval_ms: int,
) -> list[str]:
    frames: list[str] = []
    for index in range(sample_count):
        if index > 0 and sample_interval_ms:
            _runtime_wait(runtime, sample_interval_ms)
        name = f"visual-{capture['stage']}.png" if sample_count == 1 else f"visual-{capture['stage']}-frame-{index + 1:02d}.png"
        frames.append(runtime.screenshot(name, full_page=capture["full_page"]))
    return frames


def _runtime_wait(runtime: Any, wait_ms: int) -> None:
    page = getattr(runtime, "page", None)
    if page is not None and hasattr(page, "wait_for_timeout"):
        page.wait_for_timeout(wait_ms)
        return
    time.sleep(wait_ms / 1000.0)


def _hide_capture_overlays(runtime: Any, selectors: list[str]) -> None:
    """Hide transient overlays (story/event modals) in the rendered frame before capture.

    Injects a stylesheet that sets ``display:none`` on the given selectors. This is a
    render-only change (no clicks, no game-state mutation), so it is safe for choice
    modals whose buttons would otherwise mutate state, and it survives React re-renders
    because the injected <style> is outside the app's control. A no-op for non-browser
    runtimes or when no selectors are declared.
    """
    if not selectors:
        return
    page = getattr(runtime, "page", None)
    if page is None:
        return
    css = ", ".join(selectors) + " { display: none !important; }"
    try:
        page.add_style_tag(content=css)
        _runtime_wait(runtime, 120)
    except Exception:
        pass


def _unique_frame_paths(frame_paths: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for path in frame_paths:
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        unique.append(path)
    return unique


def _write_contact_sheet(frame_paths: list[Path], output_path: Path) -> None:
    images = [Image.open(path).convert("RGB") for path in frame_paths]
    try:
        widths = [image.width for image in images]
        heights = [image.height for image in images]
        cell_width = max(widths)
        cell_height = max(heights)
        cols = min(3, len(images))
        rows = (len(images) + cols - 1) // cols
        gutter = 8
        sheet = Image.new("RGB", (cols * cell_width + (cols - 1) * gutter, rows * cell_height + (rows - 1) * gutter), color=(24, 24, 24))
        for index, image in enumerate(images):
            row = index // cols
            col = index % cols
            x = col * (cell_width + gutter) + (cell_width - image.width) // 2
            y = row * (cell_height + gutter) + (cell_height - image.height) // 2
            sheet.paste(image, (x, y))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sheet.save(output_path)
    finally:
        for image in images:
            image.close()


def _visual_audit_cache_key(
    *,
    prompt: str,
    oracle: dict[str, Any],
    golden_image: Path,
    frame_paths: list[Path],
    cascade: CascadePolicyConfig,
    judge_backend: str,
) -> str:
    payload = {
        "cache_version": VISUAL_AUDIT_CACHE_VERSION,
        "prompt_version": VISUAL_AUDIT_PROMPT_VERSION,
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "oracle_hash": hashlib.sha256(json.dumps(oracle, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "golden_hash": hashlib.sha256(golden_image.read_bytes()).hexdigest(),
        "frame_hashes": [hashlib.sha256(path.read_bytes()).hexdigest() for path in frame_paths],
        "cascade_policy": cascade.cascade_policy,
        "judge_backend": judge_backend,
        "full_model": _visual_model_id(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _can_use_global_cache(cascade: CascadePolicyConfig, judge_backend: str) -> bool:
    return judge_backend != "fake" and cascade.cache_mode in {"global", "report+global"}


def _global_cache_path(cache_key: str) -> Path:
    return Path.cwd() / ".warptest-cache" / "visual-judge" / f"{cache_key}.json"


def _read_global_cache(cache_key: str) -> dict[str, Any] | None:
    path = _global_cache_path(cache_key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("cache_version") != VISUAL_AUDIT_CACHE_VERSION or not isinstance(data.get("final_verdict"), dict):
        return None
    return data


def _write_global_cache(cache_key: str, payload: dict[str, Any]) -> bool:
    path = _global_cache_path(cache_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


def _run_visual_judge(
    *,
    prompt: str,
    live_judge_path: Path,
    golden_image: Path,
    audit: dict[str, Any],
) -> dict[str, Any]:
    if deterministic_visual_only():
        raise RuntimeError(
            "the legacy single-image visual judge was reached while "
            f"{DETERMINISTIC_VISUAL_ONLY_ENV}=1; this path has no deterministic "
            "fallback, so the caller must not request visual auditing here"
        )
    verdict = VisualJudgeClient.from_env().judge(
        prompt=prompt, live_screenshot=live_judge_path, golden_image=golden_image
    )
    audit["full_verdict"] = verdict
    audit["final_verdict_source"] = audit["judge_backend"]
    return verdict


def _visual_model_id() -> str:
    return os.environ.get("WARPTEST_VISUAL_JUDGE_MODEL") or DEFAULT_VISUAL_JUDGE_MODEL


def _visual_result_payload(
    verdict: dict[str, Any],
    *,
    oracle: dict[str, Any],
    live_judge_path: Path,
    prompt_path: Path,
    raw_response_path: Path,
    quality: dict[str, Any],
    audit: dict[str, Any],
) -> dict[str, Any]:
    return {
        "label": verdict["label"],
        "expected_label": oracle["expected_label"],
        "confidence": verdict["confidence"],
        "observed": verdict["observed"],
        "rationale": verdict["rationale"],
        "rubric_failures": verdict["rubric_failures"],
        "judge_model": verdict["judge_model"],
        "live_screenshot": str(live_judge_path),
        "live_source": audit.get("live_source", "runtime_capture"),
        "golden_image": oracle["golden_image"],
        "prompt_path": str(prompt_path),
        "raw_response_path": str(raw_response_path),
        "quality": quality,
        "visual_audit": audit,
    }


def screenshot_quality(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    unique_byte_values = len(set(data))
    pixel_range = 0
    image_size = None
    try:
        with Image.open(path) as image:
            image_size = list(image.size)
            extrema = image.convert("RGB").getextrema()
            pixel_range = max(high - low for low, high in extrema)
    except Exception:
        pass
    return {
        "size_bytes": len(data),
        "unique_byte_values": unique_byte_values,
        "image_size": image_size,
        "pixel_range": pixel_range,
        "informative": len(data) >= 2_000 and unique_byte_values >= 32 and pixel_range >= 8,
    }


def build_visual_prompt(
    *,
    spec: dict[str, Any],
    oracle: dict[str, Any],
    report: RunReport,
    visual_audit: dict[str, Any] | None = None,
) -> str:
    phases = {
        "checkpoint_synthesis": report.checkpoint_synthesis.status,
        "checkpoint_restoration": report.checkpoint_restoration.status,
        "validation": report.validation.status,
        "smoke_execution": report.smoke_execution.status,
        "semantic_oracle": report.oracle_result.status,
        "target_validated_before_smoke": report.target_validated_before_smoke,
    }
    semantic_evidence = _semantic_phase_evidence(report)
    payload = {
        "task": "Judge whether the live screenshot or rendered evidence card visually satisfies the oracle.",
        "output_contract": {
            "label": "correct|incorrect|uncertain",
            "confidence": "number from 0.0 to 1.0",
            "observed": "non-empty short visual summary",
            "rationale": "non-empty short reason grounded in the screenshots and rubric",
            "rubric_failures": (
                "non-empty list of unmet must/must_not items when label=incorrect; "
                "empty list when label=correct"
            ),
        },
        "instructions": [
            "You receive exactly two images: first the golden oracle screenshot, then the live screenshot to judge.",
            "When the live image is a contact sheet, treat it as multiple sampled frames from the same run and judge the run by the clearest visible frame evidence.",
            "Use the golden screenshot as the binding visual reference for required visible game or software state, HUD elements, materials, evidence fields, and coarse layout.",
            "Compare the live screenshot against both the golden screenshot and the textual rubric.",
            "Visible differences from the golden screenshot in required objects, HUD state, hotbar contents, materials, or structure layout should make the label incorrect unless the difference is explicitly listed under oracle.rubric.ignore.",
            semantic_evidence["instruction"],
            "Do not treat semantic status as proof that the live screenshot visually matches the golden image; the final label must be justified by visible scene and UI evidence.",
            "Do not cite successful semantic phases as positive evidence for label=correct; cite visible pixels and rubric matches instead.",
            "Do not fail for differences explicitly listed under oracle.rubric.ignore.",
            "Base observations only on visible pixels in the supplied screenshots; do not infer or invent unseen error pages, dialogs, logs, or browser screens.",
            "Use label=correct only when the current visible game or software state supports the described oracle.",
            "For OpenRA, an idle clean-launch RTS scene with rendered terrain and in-game HUD/sidebar/minimap/command UI is valid live gameplay, even if units are not moving.",
            "Use label=incorrect for wrong screen, missing required UI/game facts, blank canvas, crash, or visible error UI.",
            "When label=incorrect, rubric_failures must name at least one concrete visible failure. When label=correct, rubric_failures must be empty.",
            "observed and rationale must both be non-empty and must support the returned label; do not put a revised or contradictory final label inside either field.",
            "Only claim crash/error UI when the live screenshot visibly contains crash/error text or a recognizable error dialog; quote the visible cue in rationale.",
            "Use label=uncertain when the screenshot is too ambiguous to decide.",
            "Return strict JSON only. Do not wrap it in Markdown.",
        ],
        "spec": {
            "name": spec.get("name"),
            "benchmark": spec.get("benchmark"),
            "target": spec.get("target"),
            "actions": spec.get("actions"),
            "assertions": semantic_assertions(spec),
        },
        "oracle": {
            "expected_label": oracle["expected_label"],
            "description": oracle["description"],
            "rubric": oracle["rubric"],
        },
        "semantic_phase_statuses": phases,
        "semantic_phase_evidence": semantic_evidence,
        "visual_audit": visual_audit or {},
    }
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _semantic_phase_evidence(report: RunReport) -> dict[str, Any]:
    check_counts = {
        "checkpoint_restoration": _phase_check_count(report.checkpoint_restoration),
        "validation": _phase_check_count(report.validation),
        "smoke_execution": _phase_check_count(report.smoke_execution),
        "semantic_oracle": _phase_check_count(report.oracle_result),
    }
    visual_relevant_count = (
        check_counts["validation"]
        + check_counts["smoke_execution"]
        + check_counts["semantic_oracle"]
    )
    if visual_relevant_count:
        instruction = (
            "Semantic checks may confirm runtime state; use them only as context. "
            "They do not override visible mismatch between the golden and live screenshots."
        )
    else:
        instruction = (
            "The validation, smoke, and semantic-oracle phases have no selected checks for this visual-only sample; "
            "do not cite them as confirmation that the visible scene or state is correct."
        )
    return {
        "phase_check_counts": check_counts,
        "visual_relevant_check_count": visual_relevant_count,
        "instruction": instruction,
    }


def _phase_check_count(phase: PhaseResult) -> int:
    checks = phase.data.get("checks") if isinstance(phase.data, dict) else None
    return len(checks) if isinstance(checks, list) else 0


def _visual_judge_request_worker(
    queue: Any,
    api_key: str,
    base_url: str | None,
    model: str,
    extra_headers: dict[str, str] | None,
    timeout_s: float,
    max_retries: int,
    prompt: str,
    live_screenshot: str,
    golden_image: str,
) -> None:
    response_received = False
    usage_data: dict[str, Any] | None = None
    finish_reason: str | None = None
    try:
        from openai import OpenAI

        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "timeout": timeout_s,
            "max_retries": max_retries,
        }
        if base_url:
            kwargs["base_url"] = base_url
        if extra_headers:
            kwargs["default_headers"] = dict(extra_headers)
        client = OpenAI(**kwargs)
        messages = [
                {
                    "role": "system",
                    "content": "You are a strict visual smoke-test judge for source-available games and software benchmarks. Judge only what is visible in the supplied screenshots.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "text", "text": "Golden oracle screenshot:"},
                        {"type": "image_url", "image_url": {"url": image_data_url(Path(golden_image))}},
                        {"type": "text", "text": "Live screenshot to judge:"},
                        {"type": "image_url", "image_url": {"url": image_data_url(Path(live_screenshot))}},
                    ],
                },
            ]
        response = client.chat.completions.create(
            **_visual_judge_request_parameters(model, messages)
        )
        response_received = True
        response_usage = getattr(response, "usage", None)
        usage_data = cost.usage_snapshot(response_usage)
        choices = getattr(response, "choices", None)
        if not choices:
            queue.put(
                {
                    "ok": False,
                    "error": "Visual judge returned no choices.",
                    "response_received": True,
                    "usage": usage_data,
                    "finish_reason": None,
                }
            )
            return
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) if message is not None else None
        finish_reason = getattr(choices[0], "finish_reason", None)
        reasoning_observed = cost.response_has_reasoning_content(response)
        queue.put(
            {
                "ok": True,
                "content": content,
                "response_received": True,
                "usage": usage_data,
                "finish_reason": finish_reason,
                "reasoning_observed": reasoning_observed,
                "provider_reported_model": getattr(response, "model", None),
                "provider_response_id": getattr(response, "id", None),
                "system_fingerprint": getattr(
                    response, "system_fingerprint", None
                ),
            }
        )
    except Exception as error:
        error_usage = cost.extract_usage(error)
        if error_usage is not None:
            usage_data = cost.usage_snapshot(error_usage)
        queue.put(
            {
                "ok": False,
                "error": str(error),
                "error_type": type(error).__name__,
                "status_code": getattr(error, "status_code", None),
                "response_received": response_received,
                "usage": usage_data,
                "finish_reason": finish_reason,
            }
        )


def _run_visual_judge_request_isolated(
    *,
    api_key: str,
    base_url: str | None,
    model: str,
    extra_headers: dict[str, str] | None,
    timeout_s: float,
    max_retries: int,
    prompt: str,
    live_screenshot: Path,
    golden_image: Path,
) -> dict[str, Any]:
    ctx = mp.get_context("spawn")
    queue = ctx.Queue(maxsize=1)
    process = ctx.Process(
        target=_visual_judge_request_worker,
        args=(
            queue,
            api_key,
            base_url,
            model,
            extra_headers,
            timeout_s,
            max_retries,
            prompt,
            str(live_screenshot),
            str(golden_image),
        ),
    )
    process.start()
    process.join(timeout_s + 5.0)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5.0)
        raise TimeoutError(f"Visual judge {model} timed out after {timeout_s:.0f}s")
    try:
        outcome = queue.get_nowait()
    except queue_module.Empty as error:
        raise VisualOracleError(f"Visual judge process exited without a result (exitcode={process.exitcode}).") from error
    return outcome


class VisualJudgeClient:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None,
        model: str,
        extra_headers: dict[str, str] | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url
        self.api_key = api_key
        self.extra_headers = dict(extra_headers or {})
        timeout = float(os.environ.get("WARPTEST_VISUAL_JUDGE_TIMEOUT", "300"))
        # ``judge`` owns the explicit, metered retry loop.  Keep the SDK child
        # single-shot so hidden transport retries cannot bypass usage accounting.
        max_retries = int(os.environ.get("WARPTEST_VISUAL_JUDGE_MAX_RETRIES", "0"))
        self._client_provided = client is not None
        if client is None:
            from openai import OpenAI

            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": timeout,
                "max_retries": max_retries,
            }
            if base_url:
                kwargs["base_url"] = base_url
            if self.extra_headers:
                kwargs["default_headers"] = dict(self.extra_headers)
            client = OpenAI(**kwargs)
        self.client = client
        self.timeout_s = timeout
        self.max_retries = max_retries
        self.attempts = max(1, int(os.environ.get("WARPTEST_VISUAL_JUDGE_ATTEMPTS", "3")))

    @classmethod
    def from_env(cls) -> "VisualJudgeClient | FakeVisualJudgeClient":
        # Imported lazily because providers imports model_bench, whose runtime
        # dependency graph reaches this visual module during initialization.
        from .providers import resolve_model

        fake = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
        if fake:
            return FakeVisualJudgeClient(fake)
        default_api_key = os.environ.get("WARPTEST_VISUAL_JUDGE_API_KEY") or os.environ.get(
            "MODELSCOPE_API_KEY", ""
        )
        model_spec = _visual_model_id()
        default_base_url = (
            os.environ.get("WARPTEST_VISUAL_JUDGE_BASE_URL")
            or os.environ.get("MODELSCOPE_BASE_URL")
            or DEFAULT_VISUAL_JUDGE_BASE_URL
        )
        default_api_key_env = (
            "WARPTEST_VISUAL_JUDGE_API_KEY"
            if os.environ.get("WARPTEST_VISUAL_JUDGE_API_KEY")
            else "MODELSCOPE_API_KEY"
        )
        try:
            model, provider = resolve_model(
                model_spec,
                default_base_url=default_base_url,
                default_api_key_env=default_api_key_env,
            )
        except ValueError as error:
            raise VisualOracleError(str(error)) from error
        if provider.name == "default":
            api_key = default_api_key
        else:
            api_key = os.environ.get(provider.api_key_env, "")
        base_url = provider.base_url
        missing = []
        if not api_key:
            missing.append(
                provider.api_key_env
                if provider.name != "default"
                else "WARPTEST_VISUAL_JUDGE_API_KEY or MODELSCOPE_API_KEY"
            )
        if missing:
            raise VisualOracleError("Missing visual judge environment: " + ", ".join(missing))
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            extra_headers=provider.extra_headers,
        )

    def judge(self, *, prompt: str, live_screenshot: Path, golden_image: Path) -> dict[str, Any]:
        start = time.perf_counter()
        last_error: Exception | None = None
        judge_messages = [
            {
                "role": "system",
                "content": "You are a strict visual smoke-test judge for source-available games and software benchmarks. Judge only what is visible in the supplied screenshots.",
            },
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": "Golden oracle screenshot:"},
                    {"type": "image_url", "image_url": {"url": image_data_url(golden_image)}},
                    {"type": "text", "text": "Live screenshot to judge:"},
                    {"type": "image_url", "image_url": {"url": image_data_url(live_screenshot)}},
                ],
            },
        ]
        for attempt in range(self.attempts):
            attempt_requested_at = datetime.now(timezone.utc).isoformat()
            attempt_start = time.perf_counter()
            try:
                attempt_timeout_s = bounded_task_timeout_s(self.timeout_s)
                response_error: str | None = None
                if self._client_provided:
                    try:
                        request_client = (
                            self.client.with_options(timeout=attempt_timeout_s)
                            if current_task_deadline() is not None
                            and hasattr(self.client, "with_options")
                            else self.client
                        )
                        with _wall_clock_timeout(attempt_timeout_s, f"Visual judge {self.model}"):
                            response = request_client.chat.completions.create(
                                **_visual_judge_request_parameters(
                                    self.model, judge_messages
                                )
                            )
                    except Exception as request_error:
                        try:
                            cost.record_error_usage_call(
                                self.model,
                                error=request_error,
                                latency_ms=int((time.perf_counter() - attempt_start) * 1000),
                                kind="visual_judge",
                                messages=judge_messages,
                                requested_at=attempt_requested_at,
                            )
                        except cost.UsageAccountingError as accounting_error:
                            raise accounting_error from request_error
                        raise_task_timeout_if_expired(request_error)
                        raise
                    choices = getattr(response, "choices", None)
                    if not choices:
                        response_error = "Visual judge returned no choices."
                        content = None
                        finish_reason = None
                    else:
                        message = getattr(choices[0], "message", None)
                        content = getattr(message, "content", None) if message is not None else None
                        finish_reason = getattr(choices[0], "finish_reason", None)
                    usage_value = getattr(response, "usage", None)
                    reasoning_observed = cost.response_has_reasoning_content(response)
                    provider_reported_model = getattr(response, "model", None)
                    provider_response_id = getattr(response, "id", None)
                    system_fingerprint = getattr(
                        response, "system_fingerprint", None
                    )
                else:
                    try:
                        outcome = _run_visual_judge_request_isolated(
                            api_key=self.api_key,
                            base_url=self.base_url,
                            model=self.model,
                            extra_headers=self.extra_headers,
                            timeout_s=attempt_timeout_s,
                            max_retries=self.max_retries,
                            prompt=prompt,
                            live_screenshot=live_screenshot,
                            golden_image=golden_image,
                        )
                    except Exception as request_error:
                        try:
                            cost.record_error_usage_call(
                                self.model,
                                error=request_error,
                                latency_ms=int((time.perf_counter() - attempt_start) * 1000),
                                kind="visual_judge",
                                messages=judge_messages,
                                requested_at=attempt_requested_at,
                            )
                        except cost.UsageAccountingError as accounting_error:
                            raise accounting_error from request_error
                        raise_task_timeout_if_expired(request_error)
                        raise
                    content = outcome.get("content")
                    finish_reason = outcome.get("finish_reason")
                    usage_value = outcome.get("usage")
                    reasoning_observed = bool(outcome.get("reasoning_observed", False))
                    provider_reported_model = outcome.get(
                        "provider_reported_model"
                    )
                    provider_response_id = outcome.get("provider_response_id")
                    system_fingerprint = outcome.get("system_fingerprint")
                    if not outcome.get("ok"):
                        response_error = str(outcome.get("error") or "Visual judge failed.")
                        if not outcome.get("response_received") and usage_value is None:
                            worker_error = VisualOracleError(
                                f"{outcome.get('error_type') or 'WorkerError'}: "
                                f"{response_error}"
                            )
                            status_code = outcome.get("status_code")
                            if status_code is not None:
                                worker_error.status_code = status_code
                            cost.record_error_usage_call(
                                self.model,
                                error=worker_error,
                                latency_ms=int(
                                    (time.perf_counter() - attempt_start) * 1000
                                ),
                                kind="visual_judge",
                                messages=judge_messages,
                                requested_at=attempt_requested_at,
                            )
                            if (
                                outcome.get("error_type") in {"APIConnectionError", "ConnectError", "ConnectTimeout"}
                                and not cost.error_may_have_generated(worker_error)
                            ):
                                # Preserve the child's typed pre-generation connection
                                # failure across the process boundary after accounting.
                                worker_error.recovery = {
                                    "retryable": True,
                                    "category": "provider_transient",
                                    "error": run_context.safe_error_text(worker_error),
                                }
                            raise worker_error
                cost.record_usage_call(
                    self.model,
                    usage=usage_value,
                    content=content,
                    finish_reason=finish_reason,
                    reasoning_observed=reasoning_observed,
                    reasoning_expected=(
                        resolve_model_request_profile(self.model).reasoning_expected
                    ),
                    reasoning_disabled=(
                        resolve_model_request_profile(self.model).profile
                        == "thinking_disabled"
                    ),
                    latency_ms=int((time.perf_counter() - attempt_start) * 1000),
                    kind="visual_judge",
                    messages=judge_messages,
                    response=None if content is None else str(content),
                    requested_at=attempt_requested_at,
                    endpoint="chat.completions",
                    provider_reported_model=provider_reported_model,
                    provider_response_id=provider_response_id,
                    system_fingerprint=system_fingerprint,
                )
                if response_error:
                    raise VisualOracleError(response_error)
                verdict = parse_visual_judge_response(content)
                verdict["judge_model"] = self.model
                verdict["latency_ms"] = int((time.perf_counter() - start) * 1000)
                verdict["attempts"] = attempt + 1
                return verdict
            except Exception as error:
                last_error = error
                if getattr(error, "usage_accounting_complete", True) is False:
                    raise
                if attempt + 1 < self.attempts:
                    time.sleep(1.5 * (attempt + 1))
        raise last_error or VisualOracleError("Visual judge failed without an error detail.")


class FakeVisualJudgeClient:
    def __init__(self, value: str) -> None:
        self.value = value
        self.model = "fake-visual-judge"

    def judge(self, *, prompt: str, live_screenshot: Path, golden_image: Path) -> dict[str, Any]:
        value = self.value.strip()
        if value.startswith("{"):
            verdict = parse_visual_judge_response(value)
        else:
            label = value if value in VISUAL_LABELS else "correct"
            verdict = parse_visual_judge_response(
                json.dumps(
                    {
                        "label": label,
                        "confidence": 1.0,
                        "observed": "Fake judge response.",
                        "rationale": "Configured by WARPTEST_VISUAL_JUDGE_FAKE.",
                        "rubric_failures": (
                            ["Configured fake incorrect verdict."]
                            if label == "incorrect"
                            else []
                        ),
                    }
                )
            )
        verdict["judge_model"] = self.model
        verdict["latency_ms"] = 0
        return verdict


def parse_visual_judge_response(content: Any) -> dict[str, Any]:
    if not isinstance(content, str) or not content.strip():
        raise VisualOracleError("Visual judge returned empty content.")
    body = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", body, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        body = fence.group(1).strip()
    try:
        data = json.loads(body)
    except json.JSONDecodeError as error:
        raise VisualOracleError(f"Visual judge did not return valid JSON: {error}") from error
    if not isinstance(data, dict):
        raise VisualOracleError("Visual judge JSON must be an object.")
    label = data.get("label")
    if label not in VISUAL_LABELS:
        raise VisualOracleError("Visual judge label must be correct, incorrect, or uncertain.")
    confidence = float(data.get("confidence", 0.0))
    confidence = max(0.0, min(1.0, confidence))
    failures = data.get("rubric_failures")
    if not isinstance(failures, list) or any(
        not isinstance(item, str) for item in failures
    ):
        raise VisualOracleError(
            "Visual judge rubric_failures must be a list of strings."
        )
    failures = [item.strip() for item in failures if item.strip()]
    observed_value = data.get("observed")
    rationale_value = data.get("rationale")
    if not isinstance(observed_value, str):
        raise VisualOracleError("Visual judge observed summary must be a string.")
    if not isinstance(rationale_value, str):
        raise VisualOracleError("Visual judge rationale must be a string.")
    observed = observed_value.strip()
    rationale = rationale_value.strip()
    if not observed:
        raise VisualOracleError("Visual judge observed summary must be non-empty.")
    if not rationale:
        raise VisualOracleError("Visual judge rationale must be non-empty.")
    if label == "incorrect" and not failures:
        raise VisualOracleError(
            "Visual judge label=incorrect requires at least one rubric failure."
        )
    if label == "correct" and failures:
        raise VisualOracleError(
            "Visual judge label=correct requires an empty rubric_failures list."
        )
    conclusion_matches = re.findall(
        r"\b(?:final\s+(?:decision|answer|label)|conclusion)\s*"
        r"(?:is|:|=)?\s*[\"']?(correct|incorrect|uncertain)\b",
        f"{observed}\n{rationale}",
        flags=re.IGNORECASE,
    )
    contradictory = [
        value.lower()
        for value in conclusion_matches
        if value.lower() != label
    ]
    if contradictory:
        raise VisualOracleError(
            "Visual judge explicit final conclusion contradicts its label."
        )
    return {
        "label": label,
        "confidence": confidence,
        "observed": observed,
        "rationale": rationale,
        "rubric_failures": failures,
    }


def image_data_url(path: Path) -> str:
    payload = visual_judge_image_payload(path)
    return payload["data_url"]


def visual_judge_max_image_side() -> int:
    raw = os.environ.get("WARPTEST_VISUAL_JUDGE_MAX_IMAGE_SIDE")
    if not raw:
        return VISUAL_JUDGE_MAX_IMAGE_SIDE
    max_side = int(raw)
    if max_side <= 0:
        raise VisualOracleError("Visual judge max image side must be positive.")
    return max_side


def visual_judge_max_image_bytes() -> int:
    raw = os.environ.get("WARPTEST_VISUAL_JUDGE_MAX_IMAGE_BYTES")
    if not raw:
        return VISUAL_JUDGE_MAX_IMAGE_BYTES
    max_bytes = int(raw)
    if max_bytes <= 0:
        raise VisualOracleError("Visual judge max image bytes must be positive.")
    return max_bytes


def visual_judge_image_metadata(path: Path, *, max_side: int | None = None) -> dict[str, Any]:
    payload = visual_judge_image_payload(path, max_side=max_side)
    return {
        "original_size": list(payload["original_size"]),
        "sent_size": list(payload["sent_size"]),
        "resized": payload["resized"],
        "sent_bytes": payload["sent_bytes"],
        "byte_limited": payload["byte_limited"],
    }


def visual_judge_image_payload(
    path: Path,
    *,
    max_side: int | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    resolved_max_side = visual_judge_max_image_side() if max_side is None else max_side
    resolved_max_bytes = (
        visual_judge_max_image_bytes() if max_bytes is None else max_bytes
    )
    if resolved_max_side <= 0:
        raise VisualOracleError("Visual judge max image side must be positive.")
    if resolved_max_bytes <= 0:
        raise VisualOracleError("Visual judge max image bytes must be positive.")

    with Image.open(path) as image:
        original_size = tuple(int(value) for value in image.size)
        sent_size = _fit_image_size(original_size, max_side=resolved_max_side)
        original_data = path.read_bytes()
        if sent_size == original_size and len(original_data) <= resolved_max_bytes:
            mime = image_mime_type(path)
            data = original_data
            byte_limited = False
        else:
            converted = image.convert("RGBA")
            flattened = Image.new("RGB", converted.size, "white")
            flattened.paste(converted, mask=converted.getchannel("A"))
            working = (
                flattened.resize(sent_size, Image.Resampling.LANCZOS)
                if sent_size != original_size
                else flattened
            )
            quality = 90
            while True:
                buffer = io.BytesIO()
                working.save(
                    buffer,
                    format="JPEG",
                    quality=quality,
                    optimize=False,
                    subsampling=2,
                )
                data = buffer.getvalue()
                if len(data) <= resolved_max_bytes:
                    break
                if quality > 50:
                    quality -= 10
                    continue
                width, height = working.size
                if max(width, height) <= 320:
                    raise VisualOracleError(
                        "Visual judge image cannot satisfy the configured byte limit."
                    )
                scale = min(0.9, (resolved_max_bytes / len(data)) ** 0.5 * 0.95)
                sent_size = (
                    max(1, round(width * scale)),
                    max(1, round(height * scale)),
                )
                working = working.resize(sent_size, Image.Resampling.LANCZOS)
                quality = 80
            mime = "image/jpeg"
            byte_limited = len(original_data) > resolved_max_bytes

    encoded = base64.b64encode(data).decode("ascii")
    return {
        "data_url": f"data:{mime};base64,{encoded}",
        "original_size": original_size,
        "sent_size": sent_size,
        "resized": sent_size != original_size,
        "sent_bytes": len(data),
        "byte_limited": byte_limited,
    }


def _fit_image_size(size: tuple[int, int], *, max_side: int) -> tuple[int, int]:
    width, height = size
    longest = max(width, height)
    if longest <= max_side:
        return size
    scale = max_side / longest
    return (max(1, round(width * scale)), max(1, round(height * scale)))


def image_mime_type(path: Path) -> str:
    mime = "image/png"
    if path.suffix.lower() in {".jpg", ".jpeg"}:
        mime = "image/jpeg"
    elif path.suffix.lower() == ".webp":
        mime = "image/webp"
    return mime
