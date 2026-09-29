from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .c1_evaluation import visual_oracle
from .models import PhaseResult, RunReport
from .visual import CascadePolicyConfig, run_visual_audit, screenshot_quality


OraclePolicyName = Literal["semantic", "visual", "adaptive", "fake-visual"]
ORACLE_POLICY_CHOICES = ("semantic", "visual", "adaptive", "fake-visual")


@dataclass(frozen=True)
class OraclePolicyConfig:
    policy: OraclePolicyName = "semantic"
    visual_sample_rate: float = 0.0
    visual_on_semantic_failure: bool = False


def oracle_policy_config(
    *,
    oracle_policy: str | OraclePolicyConfig | None = None,
    visual_required: bool = False,
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
) -> OraclePolicyConfig:
    if isinstance(oracle_policy, OraclePolicyConfig):
        return oracle_policy
    policy = oracle_policy or ("visual" if visual_required else "semantic")
    if policy not in ORACLE_POLICY_CHOICES:
        raise ValueError(f"Unsupported oracle policy: {policy}")
    if visual_sample_rate < 0.0 or visual_sample_rate > 1.0:
        raise ValueError("visual_sample_rate must be between 0.0 and 1.0")
    return OraclePolicyConfig(
        policy=policy,  # type: ignore[arg-type]
        visual_sample_rate=visual_sample_rate,
        visual_on_semantic_failure=visual_on_semantic_failure,
    )


def policy_may_capture_visual(config: OraclePolicyConfig, spec: dict[str, Any]) -> bool:
    return config.policy != "semantic" and isinstance(visual_oracle(spec), dict)


def semantic_status(report: RunReport) -> str:
    return (
        "success"
        if report.target_validated_before_smoke
        and report.smoke_execution.status == "success"
        and report.oracle_result.status == "success"
        else "failure"
    )


def apply_oracle_policy(
    *,
    config: OraclePolicyConfig,
    cascade_config: CascadePolicyConfig | None = None,
    spec: dict[str, Any],
    spec_path: str | Path,
    runtime: Any,
    report: RunReport,
) -> None:
    cascade = cascade_config or CascadePolicyConfig()
    report.oracle_policy = config.policy
    report.semantic_status = semantic_status(report)

    should_run, reason = visual_audit_decision(config=config, spec=spec, spec_path=spec_path, report=report)
    report.visual_audit_reason = reason
    if not should_run:
        report.visual_oracle_result = PhaseResult(
            "skipped",
            visual_skip_detail(reason),
            {
                "oracle_policy": config.policy,
                "visual_audit_reason": reason,
                "visual_audit": {
                    "cascade_policy": cascade.cascade_policy,
                    "judge_backend": "skipped",
                    "sample_count": cascade.sample_count,
                    "sample_interval_ms": cascade.sample_interval_ms,
                    "visual_sample_rate": config.visual_sample_rate,
                    "cache_hit": False,
                    "global_cache_written": False,
                    "frame_paths": [],
                    "contact_sheet_path": None,
                    "full_verdict": None,
                    "final_verdict_source": None,
                    "audit_decision_reason": reason,
                },
            },
        )
        report.visual_audit_status = "skipped"
        return

    with _fake_visual_env(config.policy):
        run_visual_audit(
            spec=spec,
            spec_path=spec_path,
            runtime=runtime,
            report=report,
            required=True,
            cascade_config=cascade,
            oracle_policy=config.policy,
            visual_audit_reason=reason,
            visual_sample_rate=config.visual_sample_rate,
        )
    report.visual_audit_status = report.visual_oracle_result.status
    report.visual_audit_reason = reason
    report.visual_oracle_result.data.setdefault("oracle_policy", config.policy)
    report.visual_oracle_result.data.setdefault("visual_audit_reason", reason)

    if report.semantic_status != "success":
        report.overall_status = "failure"
    elif report.visual_oracle_result.status == "failure":
        report.overall_status = "failure"


def visual_audit_decision(
    *,
    config: OraclePolicyConfig,
    spec: dict[str, Any],
    spec_path: str | Path,
    report: RunReport,
) -> tuple[bool, str]:
    if config.policy == "semantic":
        return False, "semantic_policy"
    if not isinstance(visual_oracle(spec), dict):
        if config.policy in {"visual", "fake-visual"}:
            return True, "missing_visual_oracle"
        return False, "missing_visual_oracle"
    if report.semantic_status != "success" and not config.visual_on_semantic_failure:
        return False, "skipped_semantic_failed"
    if config.policy in {"visual", "fake-visual"}:
        return True, "forced"
    if adaptive_risk_reason(report):
        return True, "risk_trigger"
    if _sample_selected(spec_path, config.visual_sample_rate):
        return True, "sampled"
    return False, "not_selected"


def adaptive_risk_reason(report: RunReport) -> str | None:
    if report.console_errors:
        return "console_errors"
    if report.benchmark in {"SpaceRPG", "OpenRA", "jynew", "pal3"} or report.profile in {"SpaceRPGProfile", "OpenRAProfile", "JynewProfile", "Pal3Profile"}:
        return "high_risk_runtime"
    if _weak_render_readiness(report.render_readiness_signal):
        return "weak_render_readiness"
    if _soft_lock_signal(report.soft_lock_signal):
        return "soft_lock_signal"
    if _noninformative_existing_screenshot(report.screenshots):
        return "noninformative_screenshot"
    return None


def visual_skip_detail(reason: str) -> str:
    details = {
        "semantic_policy": "Oracle policy is semantic-only; visual audit was not requested.",
        "skipped_semantic_failed": "Semantic oracle failed; visual audit was skipped by policy.",
        "missing_visual_oracle": "Spec does not declare visual_oracle.",
        "not_selected": "Adaptive visual audit did not select this semantic pass.",
    }
    return details.get(reason, f"Visual audit skipped: {reason}.")


def _sample_selected(spec_path: str | Path, rate: float) -> bool:
    if rate <= 0.0:
        return False
    if rate >= 1.0:
        return True
    digest = hashlib.sha256(str(Path(spec_path).expanduser().resolve()).encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < rate


def _weak_render_readiness(signal: dict[str, Any]) -> bool:
    if not signal:
        return False
    canvas = signal.get("canvas") if isinstance(signal.get("canvas"), dict) else signal
    if not isinstance(canvas, dict):
        return False
    if canvas.get("present") is False or canvas.get("visible") is False:
        return True
    if canvas.get("render_heartbeat_changed") is False:
        return True
    nonblank = canvas.get("nonblank_sample")
    return isinstance(nonblank, dict) and nonblank.get("nonblank") is False


def _soft_lock_signal(signal: dict[str, Any]) -> bool:
    if not signal:
        return False
    suspicious_keys = ("game_over_screen", "notification_error", "generating_event")
    if any(signal.get(key) is True for key in suspicious_keys):
        return True
    if signal.get("canvas_present") is False:
        return True
    actionable = signal.get("actionable_buttons")
    return isinstance(actionable, int) and actionable == 0 and signal.get("dialogue_open") is not True


def _noninformative_existing_screenshot(paths: list[str]) -> bool:
    for value in paths:
        path = Path(value)
        if not path.exists():
            continue
        try:
            if not screenshot_quality(path)["informative"]:
                return True
        except Exception:
            continue
    return False


@contextmanager
def _fake_visual_env(policy: str):
    if policy != "fake-visual":
        yield
        return
    previous = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
    if previous is None:
        os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
        else:
            os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = previous
