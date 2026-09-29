"""Coordinator-side execution envelope for one C1-Lazarus trial.

The policy/runtime adapter returns only raw observations.  This module owns the
two after-oracle visual decisions, the fresh P2P subset, provenance verification,
and the final pure grading call.  It is intentionally usable by the Phase-0
orchestrator without placing defect metadata or a verdict in a model payload.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from .lazarus_grading import (
    LazarusTrialEvidence,
    VisualJudgement,
    grade_lazarus_p2p_only_trial,
    grade_lazarus_trial,
)
from .lazarus_orchestrator import LazarusTrialKey
from .lazarus_p2p_only_contract import REAL_START_CONSTRUCTION_MODES
from .lazarus_upstream import P2P_CHECKPOINT_KINDS
from .lazarus_versions import LazarusVersionArtifact, tree_fingerprint
from .visual import run_visual_pair_audit


_FINAL_VERDICT_FIELDS = frozenset(
    {"start_success", "goal_success", "defect_exposed", "false_alarm"}
)
_SHA256_LENGTH = 64
_P2P_TIMEOUT_S = 300.0
_VISUAL_JUDGE_BINDING_LOCK = RLock()


@dataclass(frozen=True)
class LazarusRuntimeObservation:
    """Raw data emitted by a trusted runtime adapter after one policy attempt.

    ``goal_checks_ok`` and ``defect_signature_ok`` are coordinator-side semantic
    observations, not a policy-provided success verdict.  The adapter must not
    emit the final C1-Lazarus verdict fields; they are computed below only after
    P2P and both visual decisions exist.
    """

    complete: bool
    runtime_restored: bool
    before_semantic_ok: bool | None
    goal_checks_ok: bool | None
    defect_signature_ok: bool | None
    invariants_ok: bool | None
    before_screenshot: Path | None
    after_screenshot: Path | None
    checkpoint_sha256: str
    synthesis_cache_hit: bool | None
    actual_synthesis_calls: int
    checkpoint_reuse_success: bool | None
    treatment_tokens: int | float
    treatment_time_ms: int | float
    trace: Mapping[str, Any]
    # This trusted, coordinator-only callback is intentionally omitted from
    # serialized reports.  It recreates the Start in a disposable P2P clone
    # after the runtime has produced its observation.
    p2p_restore_adapter: Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]] | None = None
    # ``initial_screenshot`` is the raw application state before either arm
    # attempts to construct Start.  Older/formal adapters may omit it; the
    # P2P-only v2 executor requires it at its own validation seam.
    initial_screenshot: Path | None = None
    start_construction_mode: str | None = None
    start_semantic_evidence: Mapping[str, Any] | None = None
    start_evaluated: bool | None = None
    start_gate_visual: LazarusVisualEvidence | None = None


@dataclass(frozen=True)
class LazarusVisualEvidence:
    """Independent visual judgements over one before and one after frame."""

    before: VisualJudgement
    fixed_after: VisualJudgement
    buggy_after: VisualJudgement
    audits: Mapping[str, Any]


RuntimeRunner = Callable[[], LazarusRuntimeObservation]
VisualRunner = Callable[[LazarusRuntimeObservation, bool], LazarusVisualEvidence]
P2PRunner = Callable[[], Mapping[str, Any]]


def run_lazarus_trial(
    *,
    key: LazarusTrialKey,
    task_id: str,
    defect_locus: str,
    artifact: LazarusVersionArtifact,
    per_trial_p2p_ids: Sequence[str],
    version_switch_ms: int | float,
    runtime_runner: RuntimeRunner,
    visual_runner: VisualRunner,
    p2p_runner: P2PRunner,
    grading_mode: str = "semantic_visual",
) -> dict[str, Any]:
    """Execute an already-selected trial and return an auditable coordinator record.

    P2P is deliberately invoked only after the runtime reports a restored
    Start and the same after screenshot has been judged against both registered
    visual oracles.  A Start failure records the P2P subset as
    ``not_executed_start_failed`` along with both after verdicts; it never
    invents a signature from missing evidence.  Any malformed observation
    becomes an incomplete record; callers can pass it to
    :func:`run_lazarus_phase0`, which stops the pilot.
    """

    if grading_mode not in {"semantic_visual", "p2p_only_visual"}:
        raise ValueError("unknown Lazarus grading mode")
    if grading_mode == "p2p_only_visual" and not key.task_id.startswith("lzp-"):
        raise ValueError("P2P-only visual grading requires an lzp-* task")
    if grading_mode == "semantic_visual" and key.task_id.startswith("lzp-"):
        raise ValueError("lzp-* tasks require P2P-only visual grading")
    failures: list[str] = []
    if (
        isinstance(version_switch_ms, bool)
        or not isinstance(version_switch_ms, (int, float))
        or not math.isfinite(float(version_switch_ms))
        or float(version_switch_ms) < 0
    ):
        failures.append("version_switch_ms is invalid")
    if task_id != key.task_id:
        failures.append("task identity drift")
    if defect_locus not in {"at_goal", "on_path"}:
        failures.append("defect_locus is invalid")
    if artifact.task_id != key.task_id or artifact.version != key.version:
        failures.append("version artifact identity drift")
    provenance_validation_source = "live_tree_fingerprint"
    if not _sha256(artifact.tree_sha256):
        failures.append("registered artifact provenance SHA is invalid")
    else:
        attestation = artifact.build_identity.get("tree_fingerprint_attestation")
        attested = bool(
            isinstance(attestation, Mapping)
            and attestation.get("schema")
            == "c1-lazarus-materialized-pair-cache-receipt-v1"
            and attestation.get("task_id") == artifact.task_id
            and _sha256(str(attestation.get("receipt_sha256") or ""))
            and isinstance(attestation.get("version_provenance"), Mapping)
            and attestation["version_provenance"].get(artifact.version)
            == artifact.tree_sha256
        )
        if attested:
            provenance_validation_source = "scoped_materialization_receipt_v1"
        else:
            try:
                observed_tree = tree_fingerprint(artifact.root)
            except Exception as error:  # noqa: BLE001 - fail closed at the provenance seam
                observed_tree = None
                failures.append(f"artifact fingerprint could not be computed: {type(error).__name__}")
            if observed_tree is not None and observed_tree != artifact.tree_sha256:
                failures.append("artifact tree fingerprint drift")

    observation: LazarusRuntimeObservation | None = None
    failure_usage: dict[str, Any] | None = None
    try:
        candidate = runtime_runner()
        if not isinstance(candidate, LazarusRuntimeObservation):
            raise TypeError("runtime runner did not return LazarusRuntimeObservation")
        observation = candidate
        _validate_observation(
            observation,
            failures,
            require_semantic_after=grading_mode != "p2p_only_visual",
            require_real_start=grading_mode == "p2p_only_visual",
        )
    except Exception as error:  # noqa: BLE001 - an execution failure is terminal evidence
        failure_usage = _runtime_failure_usage(error)
        failures.append(f"runtime execution failed: {type(error).__name__}: {error}")

    visuals = _empty_visuals()
    p2p = _not_executed_p2p(task_id=key.task_id, version=key.version)
    if observation is None or observation.complete is not True:
        failures.append("runtime observation is incomplete")
    elif observation.runtime_restored is not True:
        failures.append("runtime was not restored before evaluation")
    else:
        evaluate_after = observation.before_semantic_ok is True
        try:
            visuals = visual_runner(observation, evaluate_after=evaluate_after)
            if not isinstance(visuals, LazarusVisualEvidence):
                raise TypeError("visual runner did not return LazarusVisualEvidence")
            _validate_visuals(observation, visuals, failures)
        except Exception as error:  # noqa: BLE001 - visual evidence is fail-closed
            failures.append(f"visual evaluation failed: {type(error).__name__}: {error}")
            visuals = _empty_visuals()
        if evaluate_after and visuals.before.ok is True:
            # The P2P clone is intentionally after runtime restoration and
            # visual capture.  It is not a substitute for a Start that failed
            # either half of the public semantic/visual contract.
            try:
                p2p = dict(p2p_runner())
                _validate_p2p(
                    p2p,
                    task_id=key.task_id,
                    version=key.version,
                    expected_ids=tuple(str(value) for value in per_trial_p2p_ids),
                    expected_checkpoint_sha256=observation.checkpoint_sha256,
                    expected_checkpoint_kind=_p2p_checkpoint_kind(key.arm),
                    failures=failures,
                )
            except Exception as error:  # noqa: BLE001 - subprocess/infrastructure evidence is terminal
                failures.append(f"per-trial P2P execution failed: {type(error).__name__}: {error}")
                p2p = _not_executed_p2p(task_id=key.task_id, version=key.version)

    p2p_ok = _p2p_passed(
        p2p,
        tuple(str(value) for value in per_trial_p2p_ids),
        checkpoint_sha256=(observation.checkpoint_sha256 if observation is not None else None),
        checkpoint_kind=_p2p_checkpoint_kind(key.arm),
    )
    evidence = LazarusTrialEvidence(
        version=key.version,
        before_semantic_ok=(
            observation.before_semantic_ok if observation is not None else None
        ),
        before_visual_ok=visuals.before.ok,
        goal_checks_ok=(observation.goal_checks_ok if observation is not None else None),
        defect_signature_ok=(
            observation.defect_signature_ok if observation is not None else None
        ),
        invariants_ok=(observation.invariants_ok if observation is not None else None),
        fixed_after_visual=visuals.fixed_after,
        buggy_after_visual=visuals.buggy_after,
        per_trial_p2p_ok=p2p_ok,
    )
    grade = (
        grade_lazarus_p2p_only_trial(evidence)
        if grading_mode == "p2p_only_visual"
        else grade_lazarus_trial(evidence)
    )
    failures.extend(grade.failures)
    complete = not failures and grade.complete
    runtime_payload = _runtime_payload(observation)
    if failure_usage is not None:
        runtime_payload["failure_usage"] = failure_usage
    report = {
        "task_id": key.task_id,
        "arm": key.arm,
        "model": key.model,
        "seed": key.seed,
        "version": key.version,
        "defect_locus": defect_locus,
        "grading_mode": grading_mode,
        "semantic_evidence": (
            "not_applicable_p2p_only"
            if grading_mode == "p2p_only_visual"
            else "registered_hidden_semantic_assertions"
        ),
        "start_protocol": (
            "real_cold_start_v1"
            if grading_mode == "p2p_only_visual"
            else None
        ),
        "start_construction_mode": (
            observation.start_construction_mode if observation is not None else None
        ),
        "start_semantic_evidence": (
            dict(observation.start_semantic_evidence or {})
            if observation is not None
            else {}
        ),
        "start_evaluated": (
            observation.start_evaluated if observation is not None else False
        ),
        "defect_evidence_source": (
            "dual_after_visual_goldens"
            if grading_mode == "p2p_only_visual"
            else "semantic_and_dual_after_visual"
        ),
        "complete": complete,
        "outcome_reason": "policy_closed_application_before_start" if observation is not None and _policy_exit_before_start(observation) else None,
        "provenance_valid": not any(
            failure.startswith("artifact ") or failure.startswith("registered artifact")
            for failure in failures
        ),
        "provenance_sha256": artifact.tree_sha256,
        "provenance_validation_source": provenance_validation_source,
        "checkpoint_sha256": (
            observation.checkpoint_sha256 if observation is not None else ""
        ),
        "synthesis_cache_hit": (
            observation.synthesis_cache_hit if observation is not None else None
        ),
        "actual_synthesis_calls": (
            observation.actual_synthesis_calls
            if observation is not None
            else (
                int(failure_usage["llm_calls"])
                if failure_usage is not None and key.arm == "lazarus_state"
                else (-1 if failure_usage is None else 0)
            )
        ),
        "checkpoint_reuse_success": (
            observation.checkpoint_reuse_success if observation is not None else None
        ),
        "treatment_tokens": (
            observation.treatment_tokens
            if observation is not None
            else (failure_usage["total_tokens"] if failure_usage is not None else 0)
        ),
        "treatment_time_ms": (
            observation.treatment_time_ms
            if observation is not None
            else (failure_usage["treatment_time_ms"] if failure_usage is not None else 0)
        ),
        "version_switch_ms": version_switch_ms,
        "start_success": grade.start_success,
        "goal_success": grade.goal_success,
        "defect_exposed": grade.defect_exposed,
        "invariants_ok": grade.invariants_ok,
        "per_trial_p2p_ok": grade.per_trial_p2p_ok,
        "false_alarm": grade.false_alarm,
        "grading": {
            "schema_version": 1,
            "evidence": _grading_evidence_payload(evidence),
            "result": {
                "version": grade.version,
                "start_success": grade.start_success,
                "goal_success": grade.goal_success,
                "defect_exposed": grade.defect_exposed,
                "invariants_ok": grade.invariants_ok,
                "per_trial_p2p_ok": grade.per_trial_p2p_ok,
                "false_alarm": grade.false_alarm,
                "complete": grade.complete,
                "after_statuses": dict(grade.after_statuses),
                "failures": list(grade.failures),
            },
        },
        "per_trial_p2p_evidence": p2p,
        "runtime": runtime_payload,
        "visual": {
            "before": _visual_payload(visuals.before),
            "fixed_after": _visual_payload(visuals.fixed_after),
            "buggy_after": _visual_payload(visuals.buggy_after),
            "audits": dict(visuals.audits),
        },
        "failures": list(dict.fromkeys(failures)),
    }
    return report


def judge_lazarus_visual_evidence(
    observation: LazarusRuntimeObservation,
    *,
    spec: Mapping[str, Any],
    spec_path: str | Path,
    evaluate_after: bool = True,
    judge_model: str | None = None,
    prior_before: Mapping[str, Any] | None = None,
) -> LazarusVisualEvidence:
    """Use the existing visual engine for one before and two after evaluations.

    The two after calls receive the *same* live screenshot and reuse the first
    before decision.  Their reported SHA-256 values are bound to the image bytes,
    so a callback cannot accidentally grade a fixed-after frame against the
    buggy golden (or vice versa).
    """

    if observation.before_screenshot is None:
        raise ValueError("visual evaluation requires a before screenshot")
    if evaluate_after and observation.after_screenshot is None:
        raise ValueError("after visual evaluation requires an after screenshot")
    lazarus = ((spec.get("evaluation") or {}).get("lazarus"))
    if not isinstance(lazarus, Mapping):
        raise ValueError("Lazarus visual evaluation requires evaluation.lazarus")
    goldens = lazarus.get("goldens")
    if not isinstance(goldens, Mapping):
        raise ValueError("Lazarus visual evaluation requires golden registrations")
    fixed = goldens.get("fixed")
    buggy = goldens.get("buggy")
    if not isinstance(fixed, Mapping) or not isinstance(buggy, Mapping):
        raise ValueError("Lazarus visual evaluation requires fixed and buggy after goldens")
    base = Path(spec_path).expanduser().resolve().parent
    before = _golden_path(base, goldens.get("before"), "before")
    fixed_after = _golden_path(base, fixed.get("after"), "fixed after")
    buggy_after = _golden_path(base, buggy.get("after"), "buggy after")
    fixed_description = str(fixed.get("after_description") or "").strip()
    buggy_description = str(buggy.get("after_description") or "").strip()
    if not fixed_description or not buggy_description:
        raise ValueError("Lazarus after descriptions are missing")
    judge_identity = _registered_visual_judge_identity(judge_model)
    evaluation = spec.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, Mapping) else {}
    registered_visual = evaluation.get("visual_oracle")
    registered_visual = (
        registered_visual if isinstance(registered_visual, Mapping) else {}
    )
    before_description = str(
        registered_visual.get("description") or fixed_description
    )
    before_rubric = registered_visual.get("rubric")
    with _bound_visual_judge_model(judge_model):
        fixed_audit = run_visual_pair_audit(
            live_before=observation.before_screenshot,
            live_after=observation.after_screenshot if evaluate_after else None,
            golden_before=before,
            golden_after=fixed_after,
            description=fixed_description,
            before_description=before_description,
            after_description=fixed_description,
            before_rubric=before_rubric,
            after_rubric=fixed.get("after_rubric"),
            evaluate_after=evaluate_after,
            prior_before=prior_before,
        )
    _validate_bound_judge_model(
        fixed_audit,
        expected=str(judge_identity["resolved_model"]) if judge_identity is not None else None,
        label="fixed visual audit",
    )
    before_judgement = _judgement_from_audit(
        fixed_audit.get("before"), observation.before_screenshot, before
    )
    if before_judgement.ok is not True or not evaluate_after:
        audits = {
            "fixed": fixed_audit,
            "buggy": {"status": "not_executed_start_failed"},
        }
        if judge_model is not None:
            audits["registered_judge_model"] = judge_model
            audits["registered_judge_identity"] = judge_identity
        return LazarusVisualEvidence(
            before=before_judgement,
            fixed_after=VisualJudgement(None, None, _sha256_file(fixed_after)),
            buggy_after=VisualJudgement(None, None, _sha256_file(buggy_after)),
            audits=audits,
        )
    fixed_judgement = _judgement_from_audit(
        fixed_audit.get("after"), observation.after_screenshot, fixed_after
    )
    with _bound_visual_judge_model(judge_model):
        buggy_audit = run_visual_pair_audit(
            live_before=observation.before_screenshot,
            live_after=observation.after_screenshot,
            golden_before=before,
            golden_after=buggy_after,
            description=buggy_description,
            before_description=before_description,
            after_description=buggy_description,
            before_rubric=before_rubric,
            after_rubric=buggy.get("after_rubric"),
            prior_before=(
                fixed_audit.get("before")
                if isinstance(fixed_audit.get("before"), Mapping)
                else None
            ),
        )
    _validate_bound_judge_model(
        fixed_audit,
        expected=(
            str(judge_identity["resolved_model"])
            if judge_identity is not None
            else None
        ),
        label="fixed visual audit",
    )
    _validate_bound_judge_model(
        buggy_audit,
        expected=(
            str(judge_identity["resolved_model"])
            if judge_identity is not None
            else None
        ),
        label="buggy visual audit",
    )
    audits = {"fixed": fixed_audit, "buggy": buggy_audit}
    if judge_model is not None:
        audits["registered_judge_model"] = judge_model
        audits["registered_judge_identity"] = judge_identity
    return LazarusVisualEvidence(
        before=before_judgement,
        fixed_after=fixed_judgement,
        buggy_after=_judgement_from_audit(
            buggy_audit.get("after"), observation.after_screenshot, buggy_after
        ),
        audits=audits,
    )


@contextmanager
def _bound_visual_judge_model(model: str | None):
    """Temporarily bind Lazarus visual requests to its registered judge.

    ``run_visual_pair_audit`` is shared with frozen C1 and intentionally reads
    its routing from the environment.  The optional binding leaves every C1
    caller unchanged while making the Phase-0 coordinator own a narrow,
    serially-safe model selection seam.
    """

    if model is None:
        yield
        return
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Lazarus visual judge model must be a non-empty string")
    with _VISUAL_JUDGE_BINDING_LOCK:
        previous = os.environ.get("WARPTEST_VISUAL_JUDGE_MODEL")
        os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] = model.strip()
        try:
            yield
        finally:
            if previous is None:
                os.environ.pop("WARPTEST_VISUAL_JUDGE_MODEL", None)
            else:
                os.environ["WARPTEST_VISUAL_JUDGE_MODEL"] = previous



def _validate_bound_judge_model(
    audit: Mapping[str, Any], *, expected: str | None, label: str
) -> None:
    """Reject a real visual result whose provider identity drifted.

    Fake visual judges are permitted only in non-paid tests and deliberately
    report ``fake-visual-judge``.  Every real before/after verdict must name
    the manifest-bound model so calibration cannot attest a different judge.
    """

    if expected is None or os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE"):
        return
    for stage in ("before", "after"):
        value = audit.get(stage)
        if not isinstance(value, Mapping):
            continue
        if value.get("evaluation_status") in {"not_evaluated_before_failed", "pending_phase_b"}:
            continue
        if value.get("judge_failure") is True:
            error = RuntimeError("visual judge unavailable: " + str(value.get("judge_error") or "unknown error"))
            error.recovery = value.get("judge_failure_details")
            raise error
        if (
            value.get("verdict_source") == "normalized_image_equality_v1"
            and value.get("judge_model") is None
            and isinstance(value.get("normalized_image_equality"), Mapping)
            and value["normalized_image_equality"].get("match") is True
        ):
            # Pixel-identical evidence is deliberately judge-free.  Requiring a
            # model identity here would turn the strongest deterministic visual
            # proof into a false route-drift failure.
            continue
        if (
            value.get("verdict_source") == "non_informative_frame_rejection_v1"
            and value.get("judge_model") is None
            and value.get("label") == "incorrect"
            and isinstance(value.get("quality"), Mapping)
            and value["quality"].get("informative") is False
        ):
            # A negative capture-quality verdict does not dispatch a judge.
            continue
        if value.get("judge_model") != expected:
            raise ValueError(f"{label} {stage} judge model drift")


def _registered_visual_judge_identity(model_spec: str | None) -> dict[str, str] | None:
    """Resolve the manifest tag exactly as the shared visual client will."""

    if model_spec is None:
        return None
    if not isinstance(model_spec, str) or not model_spec.strip():
        raise ValueError("Lazarus visual judge model must be a non-empty string")
    # ``VisualJudgeClient.from_env`` calls the same resolver.  Its transport
    # reports the provider-clean model (e.g. ``qwen3-vl-flash``), whereas the
    # manifest intentionally records the routing tag
    # (``qwen3-vl-flash@relay``).  Bind both forms instead of comparing two
    # different identifiers and rejecting every real audit.
    from .providers import resolve_model
    from .visual import DEFAULT_VISUAL_JUDGE_BASE_URL

    resolved, provider = resolve_model(
        model_spec,
        default_base_url=(
            os.environ.get("WARPTEST_VISUAL_JUDGE_BASE_URL")
            or os.environ.get("MODELSCOPE_BASE_URL")
            or DEFAULT_VISUAL_JUDGE_BASE_URL
        ),
        default_api_key_env=(
            "WARPTEST_VISUAL_JUDGE_API_KEY"
            if os.environ.get("WARPTEST_VISUAL_JUDGE_API_KEY")
            else "MODELSCOPE_API_KEY"
        ),
    )
    return {
        "model_spec": model_spec.strip(),
        "resolved_model": str(resolved),
        "provider": str(provider.name),
    }


def _policy_exit_before_start(observation: LazarusRuntimeObservation) -> bool:
    trace = observation.trace if isinstance(observation.trace, Mapping) else {}
    exit_evidence = trace.get("policy_exit") or {}
    action = exit_evidence.get("accepted_action") or {}
    return bool(
        observation.start_construction_mode == "agent_s_phase_a_from_cold_start"
        and observation.before_semantic_ok is False and observation.invariants_ok is False
        and observation.after_screenshot is None and trace.get("benchmark") == "luanti"
        and exit_evidence.get("phase") == "phase-a"
        and type(exit_evidence.get("exit_code")) is int and exit_evidence["exit_code"] == 0
        and isinstance(exit_evidence.get("target_pid"), int) and exit_evidence["target_pid"] > 0
        and action.get("kind") == "key" and action.get("key") == "escape"
        and (action.get("execution_receipt") or {}).get("accepted") is True
    )


def _validate_observation(
    observation: LazarusRuntimeObservation,
    failures: list[str],
    *,
    require_semantic_after: bool = True,
    require_real_start: bool = False,
) -> None:
    if observation.complete is not True:
        return
    if observation.runtime_restored is not True:
        return
    if type(observation.before_semantic_ok) is not bool:
        failures.append("runtime before_semantic_ok is missing")
        return
    if require_real_start:
        initial = observation.initial_screenshot
        if not isinstance(initial, Path) or not initial.is_file():
            failures.append("runtime raw initial screenshot is missing")
        if observation.start_construction_mode not in REAL_START_CONSTRUCTION_MODES:
            failures.append("runtime Start construction mode is invalid")
        semantic = observation.start_semantic_evidence
        if not isinstance(semantic, Mapping) or semantic.get("passed") is not observation.before_semantic_ok:
            failures.append("runtime Start semantic evidence is invalid")
        if observation.start_evaluated is not True:
            failures.append("runtime Start was not evaluated")
        if (
            isinstance(initial, Path)
            and initial.is_file()
            and isinstance(observation.before_screenshot, Path)
            and observation.before_screenshot.is_file()
            and observation.before_semantic_ok is True
            and _sha256_file(initial) == _sha256_file(observation.before_screenshot)
            and not (
                isinstance(semantic, Mapping)
                and semantic.get("already_satisfied") is True
            )
        ):
            failures.append("runtime Start was prepositioned instead of constructed")
    before = observation.before_screenshot
    if (not isinstance(before, Path) or not before.is_file()) and not _policy_exit_before_start(observation):
        failures.append("runtime before_screenshot is missing")
    if observation.before_semantic_ok is not True or (
        observation.start_gate_visual is not None and observation.start_gate_visual.before.ok is not True
    ):
        return
    names = (
        ("goal_checks_ok", "defect_signature_ok", "invariants_ok")
        if require_semantic_after
        else ("invariants_ok",)
    )
    for name in names:
        if type(getattr(observation, name)) is not bool:
            failures.append(f"runtime {name} is missing")
    after = observation.after_screenshot
    if not isinstance(after, Path) or not after.is_file():
        failures.append("runtime after_screenshot is missing")
    if not _sha256(observation.checkpoint_sha256):
        failures.append("runtime checkpoint SHA is invalid")
    if not isinstance(observation.actual_synthesis_calls, int) or observation.actual_synthesis_calls < 0:
        failures.append("runtime actual synthesis call count is invalid")
    for name in ("treatment_tokens", "treatment_time_ms"):
        value = getattr(observation, name)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) < 0
        ):
            failures.append(f"runtime {name} is invalid")
    if not isinstance(observation.trace, Mapping):
        failures.append("runtime trace is missing")
    elif _contains_final_verdict(observation.trace):
        failures.append("runtime trace contains a self-reported final verdict")


def _validate_visuals(
    observation: LazarusRuntimeObservation,
    visuals: LazarusVisualEvidence,
    failures: list[str],
) -> None:
    if observation.before_screenshot is None:
        if not (_policy_exit_before_start(observation) and visuals.before.ok is False):
            failures.append("runtime before screenshot is missing")
        return
    before_sha = _sha256_file(observation.before_screenshot)
    if visuals.before.screenshot_sha256 != before_sha:
        failures.append("before visual evidence does not bind the runtime screenshot")
    if observation.before_semantic_ok is not True or visuals.before.ok is not True:
        return
    if observation.after_screenshot is None:
        failures.append("runtime after screenshot is missing")
        return
    after_sha = _sha256_file(observation.after_screenshot)
    for name, judgement in (
        ("fixed", visuals.fixed_after),
        ("buggy", visuals.buggy_after),
    ):
        if judgement.ok is not None and judgement.screenshot_sha256 != after_sha:
            failures.append(f"{name} after visual evidence does not bind the runtime screenshot")


def _validate_p2p(
    evidence: Mapping[str, Any],
    *,
    task_id: str,
    version: str,
    expected_ids: tuple[str, ...],
    expected_checkpoint_sha256: str,
    expected_checkpoint_kind: str,
    failures: list[str],
) -> None:
    if not expected_ids or len(expected_ids) != len(set(expected_ids)):
        failures.append("per-trial P2P registration is invalid")
        return
    if evidence.get("schema_version") != 1:
        failures.append("per-trial P2P schema is invalid")
    if evidence.get("task_id") != task_id or evidence.get("version") != version:
        failures.append("per-trial P2P identity drift")
    timeout = evidence.get("timeout_s")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or float(timeout) != _P2P_TIMEOUT_S
    ):
        failures.append("per-trial P2P timeout drift")
    if not _p2p_total_duration_is_valid(evidence.get("duration_ms")):
        failures.append("per-trial P2P duration is invalid")
    _validate_p2p_checkpoint_restore(
        evidence.get("checkpoint_restore"),
        checkpoint_sha256=expected_checkpoint_sha256,
        checkpoint_kind=expected_checkpoint_kind,
        failures=failures,
    )
    rows = evidence.get("results")
    if not isinstance(rows, list):
        failures.append("per-trial P2P results are missing")
        return
    observed: dict[str, Mapping[str, Any]] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            failures.append("per-trial P2P result is malformed")
            continue
        test_id = str(row["id"])
        if test_id in observed:
            failures.append("per-trial P2P result is duplicated")
            continue
        observed[test_id] = row
    if set(observed) != set(expected_ids):
        failures.append("per-trial P2P result ids drift")
    for test_id in expected_ids:
        row = observed.get(test_id)
        if row is None:
            continue
        duration = row.get("duration_ms")
        if (
            row.get("passed") is not True
            or row.get("timed_out") is not False
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
            or float(duration) > _P2P_TIMEOUT_S * 1000
        ):
            failures.append(f"per-trial P2P did not pass for {test_id}")


def _p2p_passed(
    evidence: Mapping[str, Any],
    expected_ids: tuple[str, ...],
    *,
    checkpoint_sha256: str | None,
    checkpoint_kind: str,
) -> bool:
    if evidence.get("completed") is not True or evidence.get("passed") is not True:
        return False
    if not _p2p_total_duration_is_valid(evidence.get("duration_ms")):
        return False
    if not _p2p_checkpoint_restore_is_valid(
        evidence.get("checkpoint_restore"),
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_kind=checkpoint_kind,
    ):
        return False
    rows = evidence.get("results")
    if not isinstance(rows, list):
        return False
    by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, Mapping) and isinstance(row.get("id"), str)
    }
    return set(by_id) == set(expected_ids) and all(
        row.get("passed") is True and row.get("timed_out") is False
        for row in by_id.values()
    )


def _p2p_total_duration_is_valid(value: Any) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _p2p_checkpoint_kind(arm: str) -> str:
    if arm in {"lazarus_state", "lazarus_agent_s_synth", "lazarus_scalecua_synth"}:
        return "semantic_state"
    if arm == "lazarus_agent_s":
        return "agent_s_from_start"
    if arm == "lazarus_scalecua":
        return "scalecua_from_start"
    return ""


def _validate_p2p_checkpoint_restore(
    value: Any,
    *,
    checkpoint_sha256: str,
    checkpoint_kind: str,
    failures: list[str],
) -> None:
    if not _p2p_checkpoint_restore_is_valid(
        value,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_kind=checkpoint_kind,
    ):
        failures.append("per-trial P2P checkpoint restore evidence is invalid")


def lazarus_p2p_checkpoint_restore_is_valid(
    value: Any,
    *,
    checkpoint_sha256: str | None,
    checkpoint_kind: str,
) -> bool:
    """Validate the clone-local P2P restoration receipt at a public seam.

    The Phase-0 coordinator independently accepts executor reports, so it
    reuses this predicate instead of trusting that the executor went through
    :func:`run_lazarus_trial`.  Keeping the receipt contract in one place makes
    a missing adapter invocation fail closed in both paths.
    """

    return _p2p_checkpoint_restore_is_valid(
        value,
        checkpoint_sha256=checkpoint_sha256,
        checkpoint_kind=checkpoint_kind,
    )


def _p2p_checkpoint_restore_is_valid(
    value: Any,
    *,
    checkpoint_sha256: str | None,
    checkpoint_kind: str,
) -> bool:
    if (
        not isinstance(value, Mapping)
        or checkpoint_sha256 is None
        or not _sha256(checkpoint_sha256)
        or checkpoint_kind not in P2P_CHECKPOINT_KINDS
        or value.get("schema_version") != 1
        or value.get("applied") is not True
        or value.get("checkpoint_sha256") != checkpoint_sha256
        or value.get("checkpoint_kind") != checkpoint_kind
        or value.get("runtime_restored") is not True
        or value.get("path") != ".warptest/lazarus/checkpoint-restore.json"
    ):
        return False
    expected_payload = {
        "schema": "c1-lazarus-p2p-checkpoint-restore-v1",
        "schema_version": 1,
        "checkpoint_sha256": checkpoint_sha256,
        "checkpoint_kind": checkpoint_kind,
        "runtime_restored": True,
    }
    encoded = (
        json.dumps(expected_payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    receipt_sha256 = hashlib.sha256(encoded).hexdigest()
    if value.get("payload_sha256") != receipt_sha256:
        return False
    state = value.get("state_restore")
    return (
        isinstance(state, Mapping)
        and state.get("schema_version") == 1
        and state.get("state_restore_applied") is True
        and isinstance(state.get("adapter"), str)
        and bool(str(state.get("adapter")).strip())
        and state.get("checkpoint_sha256") == checkpoint_sha256
        and state.get("checkpoint_kind") == checkpoint_kind
        and state.get("receipt_sha256") == receipt_sha256
    )


def _grading_evidence_payload(evidence: LazarusTrialEvidence) -> dict[str, Any]:
    return {
        "version": evidence.version,
        "before_semantic_ok": evidence.before_semantic_ok,
        "before_visual_ok": evidence.before_visual_ok,
        "goal_checks_ok": evidence.goal_checks_ok,
        "defect_signature_ok": evidence.defect_signature_ok,
        "invariants_ok": evidence.invariants_ok,
        "fixed_after_visual": _visual_payload(evidence.fixed_after_visual),
        "buggy_after_visual": _visual_payload(evidence.buggy_after_visual),
        "per_trial_p2p_ok": evidence.per_trial_p2p_ok,
    }


def _visual_payload(value: VisualJudgement) -> dict[str, Any]:
    return {
        "ok": value.ok,
        "screenshot_sha256": value.screenshot_sha256,
        "oracle_sha256": value.oracle_sha256,
    }


def _runtime_payload(observation: LazarusRuntimeObservation | None) -> dict[str, Any]:
    if observation is None:
        return {"observed": False}
    return {
        "observed": observation.complete,
        "runtime_restored": observation.runtime_restored,
        "initial_screenshot": (
            str(observation.initial_screenshot)
            if observation.initial_screenshot is not None
            else None
        ),
        "initial_screenshot_sha256": _sha256_file(observation.initial_screenshot),
        "start_construction_mode": observation.start_construction_mode,
        "start_semantic_evidence": dict(observation.start_semantic_evidence or {}),
        "start_evaluated": observation.start_evaluated,
        "before_semantic_ok": observation.before_semantic_ok,
        "goal_checks_ok": observation.goal_checks_ok,
        "defect_signature_ok": observation.defect_signature_ok,
        "invariants_ok": observation.invariants_ok,
        "before_screenshot_sha256": _sha256_file(observation.before_screenshot),
        "after_screenshot_sha256": _sha256_file(observation.after_screenshot),
        "trace": dict(observation.trace),
    }


def _runtime_failure_usage(error: Exception) -> dict[str, Any] | None:
    value = getattr(error, "lazarus_usage", None)
    if not isinstance(value, Mapping):
        return None
    fields: dict[str, int | bool] = {}
    for name in ("llm_calls", "total_tokens"):
        item = value.get(name)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            return None
        fields[name] = item
    elapsed = value.get("treatment_time_ms", value.get("llm_latency_ms"))
    if (
        isinstance(elapsed, bool)
        or not isinstance(elapsed, (int, float))
        or not math.isfinite(float(elapsed))
        or float(elapsed) < 0
    ):
        return None
    fields["treatment_time_ms"] = int(elapsed)
    if value.get("usage_complete") is not True:
        return None
    fields["usage_complete"] = True
    return fields


def _empty_visuals() -> LazarusVisualEvidence:
    return LazarusVisualEvidence(
        before=VisualJudgement(None, None, None),
        fixed_after=VisualJudgement(None, None, None),
        buggy_after=VisualJudgement(None, None, None),
        audits={},
    )


def _not_executed_p2p(*, task_id: str, version: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_id": task_id,
        "version": version,
        "completed": False,
        "passed": False,
        "timeout_s": _P2P_TIMEOUT_S,
        "isolation": "not_executed_start_failed",
        "results": [],
        "failures": ["not_executed_start_failed"],
    }


def _golden_path(base: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Lazarus {label} golden path is missing")
    path = (base / value).resolve()
    if base != path and base not in path.parents:
        raise ValueError(f"Lazarus {label} golden escapes the spec directory")
    if not path.is_file():
        raise ValueError(f"Lazarus {label} golden does not exist")
    return path


def _judgement_from_audit(
    audit: Any, screenshot: Path, oracle: Path
) -> VisualJudgement:
    return VisualJudgement(
        ok=(audit.get("label") == "correct") if isinstance(audit, Mapping) else None,
        screenshot_sha256=_sha256_file(screenshot),
        oracle_sha256=_sha256_file(oracle),
    )


def _sha256_file(path: Path | None) -> str | None:
    if not isinstance(path, Path) or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == _SHA256_LENGTH
        and all(character in "0123456789abcdef" for character in value)
    )


def _contains_final_verdict(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            key in _FINAL_VERDICT_FIELDS or _contains_final_verdict(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_final_verdict(child) for child in value)
    return False


__all__ = [
    "LazarusRuntimeObservation",
    "LazarusVisualEvidence",
    "judge_lazarus_visual_evidence",
    "run_lazarus_trial",
]
