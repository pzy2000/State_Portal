from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

import yaml

from .core_experiment import grade_method_checkpoint, grade_method_checkpoint_strict
from .memory_backends import (
    MEMORY_BACKEND_CHOICES,
    MEMORY_SCOPE_CHOICES,
    MemoryBackend,
    get_memory_backend,
)
from . import run_context
from .models import CheckpointValidity, PhaseResult, RunReport
from .oracle_policy import OraclePolicyConfig, oracle_policy_config
from .providers import resolve_model
from .reports import new_report_dir, write_json
from .runner import load_spec, run_spec
from .spec_schema import validate_spec_schema
from .synthesis import (
    BENCHMARK_CONTRACTS,
    DEFAULT_SYNTHESIS_MODEL,
    OpenAICompatibleChatClient,
    SynthesisError,
    build_synthesis_agent,
    detect_private_grader_references,
    normalize_llm_checkpoint_mapping,
    parse_llm_checkpoint_output,
    _payload_for_spec as synthesis_payload_for_spec,
)
from .task_schema import (
    CORE_HIDDEN_ORACLES,
    CORE_PUBLIC_TASKS,
    MethodCheckpoint,
    PublicIssue,
    load_hidden_oracles,
    load_public_issues,
    resolve_hidden_spec,
    resolve_task_repo,
    validate_method_checkpoint,
    validate_public_tasks_do_not_reference_hidden,
)
from .visual import CascadePolicyConfig, cascade_policy_config


BROWSER_WEB_BENCHMARKS = ("2048", "SpaceRPG", "DatingSim")
CORE_BENCHMARKS = ("2048", "SpaceRPG", "DatingSim", "OpenRA", "JupyterLab", "LibreOffice", "Blender", "jynew", "pal3", "luanti")
TASK_SCOPE_CHOICES = ("browser-web", "all-core")
MEMORY_POLICY_CHOICES = ("positive-negative", "positive-only", "textual-only")
MUTATION_POLICY_CHOICES = ("source-constrained", "random", "none")
DEFAULT_PHASE3_SPLIT = Path("benchmarks/core/splits/evo_phase3.yaml")


class FormalC2IncompleteError(RuntimeError):
    """A formal C2 run cannot turn invalid provider evidence into a score."""


class ProviderOutageIncompleteError(FormalC2IncompleteError):
    """A formal run cannot turn an exhausted model gateway outage into a score."""


class UsageAccountingIncompleteError(FormalC2IncompleteError):
    """A paid model response is unusable because its usage cannot be audited."""


class ProviderConfigurationIncompleteError(FormalC2IncompleteError):
    """The configured formal model route rejected the request."""


class MemoryBaselineEvidenceInvalidError(FormalC2IncompleteError):
    """A completed phase lacks the evidence required for a memory comparison."""


def _formal_synthesis_incomplete(
    error: BaseException,
) -> FormalC2IncompleteError | None:
    """Map synthesis invalidity to an explicit formal-suite abort.

    Gateway outages remain retryable at the P5 layer for compatibility.  Missing
    usage accounting is deliberately fatal: replaying a paid but unauditable
    request can spend more money without making the original evidence valid.
    """

    if not isinstance(error, SynthesisError):
        return None
    failure_mode = error.artifacts.get("failure_mode")
    if failure_mode == "gateway_outage":
        return ProviderOutageIncompleteError(
            "temporarily unavailable: C2 model gateway outage exhausted its retry budget; "
            "the formal suite is incomplete and has no effect conclusion"
        )
    if failure_mode == "usage_accounting_incomplete":
        return UsageAccountingIncompleteError(
            "C2 provider usage accounting is incomplete; paid model evidence cannot be "
            "audited, automatic replay is forbidden, and the formal suite has no effect "
            "conclusion"
        )
    if failure_mode == "client_error":
        return ProviderConfigurationIncompleteError(
            "C2 model route rejected the configured request; the formal suite is "
            "incomplete, automatic replay is forbidden, and there is no effect "
            "conclusion"
        )
    return None


DIAGNOSIS_LABELS = {
    "schema_error",
    "target_state_mismatch",
    "hook_unavailable",
    "persistence_mismatch",
    "render_not_ready",
    "soft_locked",
    "smoke_action_failed",
    "public_invariant_failed",
    "runtime_error",
    "success",
}
PRIVATE_MEMORY_KEYWORDS = (
    "hidden",
    "strict",
    "scaffold",
    "grading",
    "target_match",
    "hidden_assertions_covered",
)
DEFAULT_PROVIDER_RETRIES = 2


@dataclass
class RepairConfig:
    enabled: bool = True
    model: str = DEFAULT_SYNTHESIS_MODEL
    base_url: str = "https://api-inference.modelscope.cn/v1"
    api_key_env: str = "MODELSCOPE_API_KEY"
    extra_headers: dict[str, str] | None = None
    temperature: float = 0.0
    max_tokens: int = 4096
    max_attempts: int = 2
    client: Any | None = None


@dataclass
class CheckpointHypothesis:
    id: str
    task_id: str
    benchmark: str
    generation: int
    spec: dict[str, Any]
    source_evidence: list[dict[str, Any]]
    inputs_read: list[str]
    payload: dict[str, Any]
    validity_class: CheckpointValidity
    generated_by: str
    parent_ids: list[str] = field(default_factory=list)
    mutation: str = "seed"
    operator_id: str = "seed"
    used_public_scaffold: bool = False
    cost: dict[str, Any] = field(default_factory=dict)

    def materialize_checkpoint(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "benchmark": self.benchmark,
            "generated_spec": copy.deepcopy(self.spec),
            "source_evidence": copy.deepcopy(self.source_evidence) or [
                {"source_path": "", "matched_symbol": self.benchmark, "rationale": "EvoPortal fallback public evidence."}
            ],
            "payload": copy.deepcopy(self.payload),
            "validity_class": self.validity_class,
            "runtime_validation": {"status": "pending", "detail": "EvoPortal candidate has not been executed yet."},
            "inputs_read": list(self.inputs_read),
            "hidden_files_read": [],
            "generated_by": self.generated_by,
            "model_generated_spec": copy.deepcopy(self.spec),
            "used_public_scaffold": self.used_public_scaffold,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "benchmark": self.benchmark,
            "generation": self.generation,
            "spec": copy.deepcopy(self.spec),
            "source_evidence": copy.deepcopy(self.source_evidence),
            "inputs_read": list(self.inputs_read),
            "payload": copy.deepcopy(self.payload),
            "validity_class": self.validity_class,
            "generated_by": self.generated_by,
            "parent_ids": list(self.parent_ids),
            "mutation": self.mutation,
            "operator_id": self.operator_id,
            "used_public_scaffold": self.used_public_scaffold,
            "cost": copy.deepcopy(self.cost),
        }


@dataclass
class ValidationDiagnosis:
    label: str
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    repair_labels: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.label not in DIAGNOSIS_LABELS:
            raise ValueError(f"Unsupported EvoPortal diagnosis label: {self.label}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MemoryEntry:
    id: str
    scope: str
    benchmark: str
    semantic_surface: str
    issue_keywords: list[str]
    strategy: str
    failure_signature: str
    checkpoint_recipe: dict[str, Any]
    public_validation_summary: dict[str, Any]
    source_symbols: list[str]
    repair_history: list[dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.5
    support_count: int = 1
    entry_type: str = "validated_recipe"
    score: float = 0.0
    text_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CandidateEvaluation:
    hypothesis: CheckpointHypothesis
    admitted: bool
    executed: bool
    diagnosis: ValidationDiagnosis
    rank: tuple[Any, ...]
    checkpoint: dict[str, Any]
    runtime_report: dict[str, Any] | None = None
    admission_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hypothesis": self.hypothesis.to_dict(),
            "admitted": self.admitted,
            "executed": self.executed,
            "diagnosis": self.diagnosis.to_dict(),
            "rank": list(self.rank),
            "checkpoint": copy.deepcopy(self.checkpoint),
            "runtime_report": copy.deepcopy(self.runtime_report),
            "admission_errors": list(self.admission_errors),
        }


@dataclass(frozen=True)
class EvoSplit:
    name: str
    train: list[str]
    heldout: list[str]


@dataclass
class OperatorMutation:
    operator_id: str
    gene: str
    spec: dict[str, Any]
    source_evidence: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class EvolutionOperator:
    operator_id: str
    gene: str
    apply: Callable[[CheckpointHypothesis, ValidationDiagnosis, PublicIssue], list[OperatorMutation]]


def filter_phase1_tasks(
    tasks: Sequence[PublicIssue],
    benchmarks: Sequence[str] | None = None,
    *,
    task_limit: int | None = None,
) -> list[PublicIssue]:
    return filter_evo_tasks(tasks, benchmarks, task_scope="browser-web", task_limit=task_limit)


def filter_evo_tasks(
    tasks: Sequence[PublicIssue],
    benchmarks: Sequence[str] | None = None,
    *,
    task_scope: str = "browser-web",
    task_ids: Sequence[str] | None = None,
    task_limit: int | None = None,
) -> list[PublicIssue]:
    if task_scope not in TASK_SCOPE_CHOICES:
        raise ValueError(f"task_scope must be one of: {', '.join(TASK_SCOPE_CHOICES)}")
    default_benchmarks = CORE_BENCHMARKS if task_scope == "all-core" else BROWSER_WEB_BENCHMARKS
    selected = set(benchmarks or default_benchmarks)
    result = [task for task in tasks if task.benchmark in selected]
    if task_ids:
        ordered_ids = [str(task_id) for task_id in task_ids]
        if len(ordered_ids) != len(set(ordered_ids)):
            raise ValueError("Evo task_ids must not contain duplicates")
        by_id = {task.id: task for task in result}
        missing = [task_id for task_id in ordered_ids if task_id not in by_id]
        if missing:
            raise ValueError(
                "Evo task_ids are missing from the selected benchmark scope: "
                + ", ".join(missing)
            )
        # Explicit ids are an ordered execution contract. C2 train order affects
        # the repository memory available to later tasks, so silently reverting
        # to public-task file order would change both treatment and resume state.
        result = [by_id[task_id] for task_id in ordered_ids]
    if task_limit is not None:
        result = result[: max(0, task_limit)]
    return result


def load_evo_split(path: str | Path = DEFAULT_PHASE3_SPLIT) -> EvoSplit:
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Evo split must be a mapping: {path}")
    train = data.get("train_task_ids")
    heldout = data.get("heldout_task_ids")
    if not isinstance(train, list) or not isinstance(heldout, list):
        raise ValueError("Evo split must define train_task_ids and heldout_task_ids lists.")
    overlap = set(train) & set(heldout)
    if overlap:
        raise ValueError(f"Evo split train/heldout overlap: {sorted(overlap)}")
    return EvoSplit(
        name=str(data.get("name", Path(path).stem)),
        train=[str(item) for item in train],
        heldout=[str(item) for item in heldout],
    )


def hypothesis_from_method_checkpoint(
    checkpoint: MethodCheckpoint | dict[str, Any],
    *,
    generation: int = 0,
    mutation: str = "seed",
) -> CheckpointHypothesis:
    data = checkpoint.to_dict() if isinstance(checkpoint, MethodCheckpoint) else copy.deepcopy(checkpoint)
    spec = data.get("generated_spec") if isinstance(data.get("generated_spec"), dict) else {}
    return _make_hypothesis(
        task_id=str(data.get("task_id", "")),
        benchmark=str(data.get("benchmark", spec.get("benchmark", ""))),
        spec=spec,
        source_evidence=[item for item in data.get("source_evidence", []) if isinstance(item, dict)],
        inputs_read=[str(item) for item in data.get("inputs_read", [])],
        generation=generation,
        generated_by=str(data.get("generated_by", "unknown")),
        parent_ids=[],
        mutation=mutation,
        used_public_scaffold=bool(data.get("used_public_scaffold")),
    )


def initial_hypotheses_from_checkpoint(checkpoint: MethodCheckpoint | dict[str, Any]) -> list[CheckpointHypothesis]:
    data = checkpoint.to_dict() if isinstance(checkpoint, MethodCheckpoint) else copy.deepcopy(checkpoint)
    scaffold_hypothesis = hypothesis_from_method_checkpoint(data, generation=0, mutation="seed_checkpoint")
    hypotheses = [scaffold_hypothesis]
    model_spec = data.get("model_generated_spec")
    if isinstance(model_spec, dict):
        generated_spec = data.get("generated_spec") if isinstance(data.get("generated_spec"), dict) else {}
        if _fingerprint(model_spec) != _fingerprint(generated_spec):
            raw_hypothesis = _make_hypothesis(
                task_id=str(data.get("task_id", "")),
                benchmark=str(data.get("benchmark", model_spec.get("benchmark", ""))),
                spec=model_spec,
                source_evidence=[item for item in data.get("source_evidence", []) if isinstance(item, dict)],
                inputs_read=[str(item) for item in data.get("inputs_read", [])],
                generation=0,
                generated_by=str(data.get("generated_by", "unknown")),
                parent_ids=[scaffold_hypothesis.id],
                mutation="raw_model_spec_from_scaffolded_seed",
                used_public_scaffold=False,
            )
            if data.get("used_public_scaffold"):
                hypotheses.insert(0, raw_hypothesis)
            else:
                hypotheses.append(raw_hypothesis)
    return hypotheses


def admit_candidate(hypothesis: CheckpointHypothesis, root: str | Path = Path.cwd()) -> list[str]:
    checkpoint = hypothesis.materialize_checkpoint()
    errors: list[str] = []
    if hypothesis.used_public_scaffold:
        errors.append("candidate uses public scaffold fallback")
    if not isinstance(checkpoint.get("model_generated_spec"), dict):
        errors.append("candidate has no executable model_generated_spec")
    errors.extend(validate_method_checkpoint(checkpoint, root))
    errors.extend(validate_spec_schema(hypothesis.spec, f"evo:{hypothesis.id}"))
    for reference in detect_private_grader_references(checkpoint):
        errors.append(f"candidate references private grader data: {reference}")
    if not _strategy_allowed(hypothesis.benchmark, hypothesis.spec):
        errors.append("candidate strategy is not allowed by the Phase 1 public contract")
    return _dedupe(errors)


def diagnose_report(report: RunReport | dict[str, Any] | Any) -> ValidationDiagnosis:
    validation_status = _phase_status(_get(report, "validation"))
    smoke_status = _phase_status(_get(report, "smoke_execution"))
    oracle_status = _phase_status(_get(report, "oracle_result"))
    restoration_status = _phase_status(_get(report, "checkpoint_restoration"))
    hook = _get(report, "hook_availability") or {}
    persistence = _get(report, "memory_persistence_diff") or {}
    render = _get(report, "render_readiness_signal") or {}
    soft_lock = _get(report, "soft_lock_signal") or {}
    post_interaction = _get(report, "post_checkpoint_interaction") or {}
    console_errors = _get(report, "console_errors") or []
    target_ok = bool(_get(report, "target_validated_before_smoke"))

    if _hook_unavailable(hook) or _mentions(_phase_detail(_get(report, "checkpoint_restoration")), "hook"):
        return ValidationDiagnosis("hook_unavailable", "Required public runtime hook was unavailable.", {"hook_availability": hook}, ["switch_strategy"])
    if restoration_status == "failure" or console_errors:
        return ValidationDiagnosis(
            "runtime_error",
            "Candidate failed during checkpoint restoration or produced console/runtime errors.",
            {"restoration_status": restoration_status, "console_errors": console_errors[:5]},
            ["repair_runtime_payload"],
        )
    if _meaningful_mapping(persistence):
        return ValidationDiagnosis("persistence_mismatch", "Runtime state and persistence/storage evidence diverged.", {"memory_persistence_diff": persistence}, ["add_persistence_fields"])
    if _render_not_ready(render):
        return ValidationDiagnosis("render_not_ready", "Runtime state was not visibly/render-ready after checkpoint restore.", {"render_readiness_signal": render}, ["add_readiness_wait"])
    if _soft_locked(soft_lock) or _post_interaction_failed(post_interaction):
        return ValidationDiagnosis("soft_locked", "Checkpoint restored into a state with blocked follow-up interaction.", {"soft_lock_signal": soft_lock, "post_checkpoint_interaction": post_interaction}, ["add_preconditions"])
    if target_ok and validation_status == "success" and smoke_status == "success" and oracle_status == "success":
        return ValidationDiagnosis("success", "Public validation, smoke action, and public assertions passed.")
    if smoke_status == "failure":
        return ValidationDiagnosis("smoke_action_failed", "Post-checkpoint smoke action failed.", {"smoke_execution": _phase_dict(_get(report, "smoke_execution"))}, ["mutate_smoke_action"])
    if validation_status == "failure" or not target_ok:
        return ValidationDiagnosis("target_state_mismatch", "Candidate's declared public target was not validated.", {"validation": _phase_dict(_get(report, "validation"))}, ["mutate_target_fields"])
    if oracle_status == "failure":
        return ValidationDiagnosis("public_invariant_failed", "Public assertion or metamorphic check failed.", {"oracle_result": _phase_dict(_get(report, "oracle_result"))}, ["mutate_public_assertions"])
    return ValidationDiagnosis("runtime_error", "Candidate failed without a more specific public diagnosis.", {"validation_status": validation_status, "smoke_status": smoke_status, "oracle_status": oracle_status}, ["inspect_report"])


def coarse_diagnose_report(report: RunReport | dict[str, Any] | Any) -> ValidationDiagnosis:
    validation_status = _phase_status(_get(report, "validation"))
    smoke_status = _phase_status(_get(report, "smoke_execution"))
    oracle_status = _phase_status(_get(report, "oracle_result"))
    target_ok = bool(_get(report, "target_validated_before_smoke"))
    if target_ok and validation_status == "success" and smoke_status == "success" and oracle_status == "success":
        return ValidationDiagnosis("success", "Public validation, smoke action, and public assertions passed.")
    return ValidationDiagnosis(
        "runtime_error",
        "Failure diagnosis disabled for this ablation.",
        {"validation_status": validation_status, "smoke_status": smoke_status, "oracle_status": oracle_status},
        ["diagnosis_disabled"],
    )


def rank_candidate(
    hypothesis: CheckpointHypothesis,
    diagnosis: ValidationDiagnosis,
    report: RunReport | dict[str, Any] | Any | None = None,
) -> tuple[Any, ...]:
    if report is None:
        return (0, 0, 0, 0, 0, 0, -1, _novelty_key(hypothesis), 0)
    declared_target = int(bool(_get(report, "target_validated_before_smoke")) or _phase_status(_get(report, "validation")) == "success")
    runtime_health = int(not (_get(report, "console_errors") or []) and _phase_status(_get(report, "checkpoint_restoration")) != "failure")
    persistence = int(not _meaningful_mapping(_get(report, "memory_persistence_diff") or {}))
    render = int(not _render_not_ready(_get(report, "render_readiness_signal") or {}))
    interaction = int(_phase_status(_get(report, "smoke_execution")) == "success" and not _post_interaction_failed(_get(report, "post_checkpoint_interaction") or {}))
    public_invariant = int(_phase_status(_get(report, "oracle_result")) == "success")
    impossible_risk = int(diagnosis.label in {"soft_locked", "persistence_mismatch"})
    cost = int(hypothesis.cost.get("executions", 1))
    return (
        declared_target,
        runtime_health,
        persistence,
        render,
        interaction,
        public_invariant,
        -impossible_risk,
        _novelty_key(hypothesis),
        -cost,
    )


def memory_entry_from_evaluation(task: PublicIssue, evaluation: CandidateEvaluation, *, scope: str = "episode") -> MemoryEntry:
    report = evaluation.runtime_report or {}
    spec = evaluation.hypothesis.spec
    target = spec.get("target") if isinstance(spec.get("target"), dict) else {}
    strategy = _candidate_strategy(spec)
    label = evaluation.diagnosis.label
    recipe = {
        "target": _scrub_private(target),
        "checkpoint": _scrub_private(spec.get("checkpoint", {})),
        "actions": _scrub_private(spec.get("actions", [])),
        "assertions": _scrub_private(spec.get("assertions", [])),
        "mutation": evaluation.hypothesis.mutation,
    }
    summary = {
        "diagnosis": label,
        "validation": _phase_status(report.get("validation")),
        "smoke": _phase_status(report.get("smoke_execution")),
        "public_assertions": _phase_status(report.get("oracle_result")),
        "target_validated_before_smoke": bool(report.get("target_validated_before_smoke")),
    }
    semantic_surface = str(target.get("kind") or (task.tags[-1] if task.tags else task.benchmark))
    entry_type = "validated_recipe" if label == "success" else "negative_contrast"
    return MemoryEntry(
        id="mem_" + hashlib.sha1(f"{task.id}:{evaluation.hypothesis.id}:{label}".encode("utf-8")).hexdigest()[:12],
        scope=scope,
        benchmark=task.benchmark,
        semantic_surface=semantic_surface,
        issue_keywords=_keywords(task.issue, task.tags),
        strategy=strategy,
        failure_signature=label,
        checkpoint_recipe=recipe,
        public_validation_summary=summary,
        source_symbols=_source_symbols(evaluation.hypothesis.source_evidence),
        repair_history=[{"from_failure": label, "candidate": evaluation.hypothesis.id, "result": "success" if label == "success" else "failure"}],
        confidence=0.9 if label == "success" else 0.45,
        support_count=1,
        entry_type=entry_type,
        text_summary=_memory_text_summary(task, evaluation),
    )


def textual_memory_entry_from_evaluation(task: PublicIssue, evaluation: CandidateEvaluation, *, scope: str = "episode") -> MemoryEntry:
    base = memory_entry_from_evaluation(task, evaluation, scope=scope)
    base.entry_type = "textual_summary"
    base.checkpoint_recipe = {}
    base.failure_signature = evaluation.diagnosis.label
    base.confidence = 0.55
    base.text_summary = _memory_text_summary(task, evaluation)
    return base


def memory_entries_from_evaluation(
    task: PublicIssue,
    evaluation: CandidateEvaluation,
    *,
    scope: str = "episode",
    policy: str = "positive-negative",
) -> list[MemoryEntry]:
    if policy == "textual-only":
        return [textual_memory_entry_from_evaluation(task, evaluation, scope=scope)]
    entry = memory_entry_from_evaluation(task, evaluation, scope=scope)
    if policy == "positive-only" and entry.entry_type != "validated_recipe":
        return []
    return [entry]


def retrieve_memory(entries: Sequence[MemoryEntry], task: PublicIssue, *, limit: int = 5, policy: str = "positive-negative") -> list[MemoryEntry]:
    scored = score_memory_entries(_filter_memory_by_policy(entries, policy), task)
    return [entry for _, entry in scored[:limit]]


def _filter_memory_by_policy(entries: Sequence[MemoryEntry], policy: str) -> list[MemoryEntry]:
    if policy == "positive-only":
        return [entry for entry in entries if entry.entry_type == "validated_recipe"]
    if policy == "textual-only":
        return [entry for entry in entries if entry.entry_type == "textual_summary"]
    return [entry for entry in entries if entry.entry_type in {"validated_recipe", "negative_contrast"}]


def _memory_text_summary(task: PublicIssue, evaluation: CandidateEvaluation) -> str:
    label = evaluation.diagnosis.label
    mutation = evaluation.hypothesis.mutation
    validation = _phase_status((evaluation.runtime_report or {}).get("validation"))
    smoke = _phase_status((evaluation.runtime_report or {}).get("smoke_execution"))
    public_assertions = _phase_status((evaluation.runtime_report or {}).get("oracle_result"))
    return (
        f"{task.id} {task.benchmark}: mutation={mutation}; diagnosis={label}; "
        f"validation={validation}; smoke={smoke}; public_assertions={public_assertions}."
    )


def score_memory_entries(entries: Sequence[MemoryEntry], task: PublicIssue) -> list[tuple[float, MemoryEntry]]:
    query_terms = _memory_query_terms(task)
    docs = [_memory_document_terms(entry) for entry in entries]
    avg_len = sum(len(doc) for doc in docs) / len(docs) if docs else 0.0
    scored: list[tuple[float, MemoryEntry]] = []
    for entry, doc in zip(entries, docs):
        if entry.benchmark != task.benchmark:
            continue
        score = _bm25_score(query_terms, doc, docs, avg_len)
        schema_bonus = _schema_key_bonus(query_terms, entry)
        total = score + schema_bonus + float(entry.confidence) * 0.1
        if total > 0:
            scored.append((round(total, 6), entry))
    scored.sort(key=lambda item: (-item[0], item[1].entry_type, item[1].id))
    return scored


def select_parent_evaluations(evaluations: Sequence[CandidateEvaluation], *, limit: int = 2) -> list[CandidateEvaluation]:
    successful_or_ranked = sorted(evaluations, key=lambda item: item.rank, reverse=True)
    selected: list[CandidateEvaluation] = []
    seen_ids: set[str] = set()
    for evaluation in successful_or_ranked:
        if evaluation.hypothesis.id in seen_ids:
            continue
        selected.append(evaluation)
        seen_ids.add(evaluation.hypothesis.id)
        if len(selected) >= limit:
            break
    seen_labels = {item.diagnosis.label for item in selected}
    for evaluation in evaluations:
        if evaluation.diagnosis.label in seen_labels or evaluation.hypothesis.id in seen_ids:
            continue
        selected.append(evaluation)
        seen_ids.add(evaluation.hypothesis.id)
        seen_labels.add(evaluation.diagnosis.label)
        if len(selected) >= limit + 2:
            break
    return selected


_EVO_PROGRESS_FILE = "evo-progress.json"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Persist an Evo checkpoint atomically.

    The Phase-3 runs can take hours.  A process interrupt must therefore leave
    either the prior complete checkpoint or the next complete checkpoint on
    disk, never a truncated JSON file that makes the phase unrecoverable.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _evo_progress_signature(
    *,
    base: Path,
    task_ids: Sequence[str],
    tasks_path: str | Path,
    oracle_path: str | Path,
    agent_kind: str,
    model: str,
    base_url: str,
    api_key_env: str,
    temperature: float,
    max_tokens: int,
    iterations: int,
    population: int,
    memory_mode: str,
    memory_backend: str,
    memory_scope: str,
    memory_in: str | Path | None,
    memory_out: str | Path | None,
    memory_policy: str,
    mutation_policy: str,
    repository_memory_writes: bool,
    memory_export: bool,
    task_scope: str,
    headless: bool,
    explore: bool,
    max_explore_steps: int,
    provider_retries: int,
    repair: bool,
    repair_max_attempts: int,
    diagnosis_enabled: bool,
    fail_on_provider_outage: bool,
    oracle_policy: OraclePolicyConfig,
    cascade_config: CascadePolicyConfig,
) -> dict[str, Any]:
    """Describe every setting that can affect a resumable Evo task result."""

    def resolved(value: str | Path | None) -> str | None:
        if value is None:
            return None
        path = Path(value).expanduser()
        return str(path if path.is_absolute() else (base / path).resolve())

    memory_in_path = Path(memory_in).expanduser() if memory_in is not None else None
    if memory_in_path is not None and not memory_in_path.is_absolute():
        memory_in_path = (base / memory_in_path).resolve()
    memory_in_sha256 = (
        hashlib.sha256(memory_in_path.read_bytes()).hexdigest()
        if memory_in_path is not None
        else None
    )

    return {
        "schema_version": 2,
        "task_ids": list(task_ids),
        "public_task_file": resolved(tasks_path),
        "hidden_oracle_file": resolved(oracle_path),
        "agent_kind": agent_kind,
        "model": model,
        "base_url": base_url.rstrip("/"),
        "api_key_env": api_key_env,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "iterations": iterations,
        "population": population,
        "memory_mode": memory_mode,
        "memory_backend": memory_backend,
        "memory_scope": memory_scope,
        "memory_in": resolved(memory_in),
        # A held-out task prefix is reusable only with the exact frozen train
        # treatment it originally saw. Binding merely the path would allow an
        # in-place replacement to mix old- and new-memory task results.
        "memory_in_sha256": memory_in_sha256,
        "memory_out": resolved(memory_out),
        "memory_policy": memory_policy,
        "mutation_policy": mutation_policy,
        "repository_memory_writes": repository_memory_writes,
        "memory_export": memory_export,
        "task_scope": task_scope,
        "headless": headless,
        "explore": explore,
        "max_explore_steps": max_explore_steps,
        "provider_retries": provider_retries,
        "repair": repair,
        "repair_max_attempts": repair_max_attempts,
        "diagnosis_enabled": diagnosis_enabled,
        "fail_on_provider_outage": fail_on_provider_outage,
        "oracle_policy": asdict(oracle_policy),
        "cascade_config": asdict(cascade_config),
    }


def _load_evo_progress(
    path: Path,
    *,
    signature: dict[str, Any],
    selected_tasks: Sequence[PublicIssue],
) -> list[dict[str, Any]]:
    """Load only a prefix-complete task checkpoint compatible with this run."""

    try:
        progress = json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        raise ValueError(f"Cannot resume unreadable Evo progress checkpoint: {path}") from error
    if not isinstance(progress, dict) or progress.get("schema_version") != 1:
        raise ValueError(f"Cannot resume unsupported Evo progress checkpoint: {path}")
    if progress.get("experiment") != "evo_synthesis_task_progress":
        raise ValueError(f"Cannot resume unrelated Evo progress checkpoint: {path}")
    if progress.get("signature") != signature:
        raise ValueError(
            f"Cannot resume Evo phase with changed settings: {path}; use a new report directory"
        )
    if progress.get("complete") is True:
        raise ValueError(
            f"Evo progress says complete but final report is absent: {path}; use a new report directory"
        )
    results = progress.get("results")
    completed_ids = progress.get("completed_task_ids")
    if not isinstance(results, list) or not isinstance(completed_ids, list):
        raise ValueError(f"Cannot resume malformed Evo progress checkpoint: {path}")
    expected_ids = [task.id for task in selected_tasks]
    restored_ids = [str(item) for item in completed_ids]
    if restored_ids != expected_ids[:len(restored_ids)] or len(results) != len(restored_ids):
        raise ValueError(
            f"Cannot resume non-prefix Evo progress checkpoint: {path}; task ordering is unsafe"
        )
    for task_id, result in zip(restored_ids, results):
        if not isinstance(result, dict) or result.get("task_id") != task_id:
            raise ValueError(f"Cannot resume malformed Evo task result for {task_id}: {path}")
    return [dict(result) for result in results]


def run_evo_synthesis_benchmark(
    root: str | Path = Path.cwd(),
    *,
    tasks_path: str | Path = CORE_PUBLIC_TASKS,
    oracle_path: str | Path = CORE_HIDDEN_ORACLES,
    agent_kind: str = "llm",
    model: str = DEFAULT_SYNTHESIS_MODEL,
    base_url: str = "https://api-inference.modelscope.cn/v1",
    api_key_env: str = "MODELSCOPE_API_KEY",
    temperature: float = 0.0,
    max_tokens: int = 8192,
    iterations: int = 2,
    population: int = 2,
    memory_mode: str = "episode",
    memory_backend: str = "stateportal",
    memory_scope: str = "intra",
    memory_in: str | Path | None = None,
    memory_out: str | Path | None = None,
    benchmarks: Sequence[str] | None = None,
    task_limit: int | None = None,
    oracle_policy: str | OraclePolicyConfig | None = "semantic",
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
    headless: bool = True,
    explore: bool = True,
    max_explore_steps: int = 12,
    provider_retries: int = DEFAULT_PROVIDER_RETRIES,
    repair: bool = True,
    repair_max_attempts: int = 2,
    task_scope: str = "browser-web",
    task_ids: Sequence[str] | None = None,
    memory_policy: str = "positive-negative",
    mutation_policy: str = "source-constrained",
    diagnosis_enabled: bool = True,
    memory_export: bool = True,
    repository_memory_writes: bool = True,
    report_dir: Path | None = None,
    resume: bool = False,
    fail_on_provider_outage: bool = False,
) -> dict[str, Any]:
    if iterations < 1:
        raise ValueError("iterations must be at least 1")
    if population < 1:
        raise ValueError("population must be at least 1")
    if memory_mode not in {"none", "episode", "repository"}:
        raise ValueError("memory_mode must be one of: none, episode, repository")
    if memory_backend not in MEMORY_BACKEND_CHOICES:
        raise ValueError(f"memory_backend must be one of: {', '.join(MEMORY_BACKEND_CHOICES)}")
    if memory_scope not in MEMORY_SCOPE_CHOICES:
        raise ValueError(f"memory_scope must be one of: {', '.join(MEMORY_SCOPE_CHOICES)}")
    if memory_policy not in MEMORY_POLICY_CHOICES:
        raise ValueError(f"memory_policy must be one of: {', '.join(MEMORY_POLICY_CHOICES)}")
    if mutation_policy not in MUTATION_POLICY_CHOICES:
        raise ValueError(f"mutation_policy must be one of: {', '.join(MUTATION_POLICY_CHOICES)}")

    base = Path(root).expanduser().resolve()
    out_dir = report_dir or new_report_dir()
    all_tasks = load_public_issues(base, tasks_path)
    selected_tasks = filter_evo_tasks(all_tasks, benchmarks, task_scope=task_scope, task_ids=task_ids, task_limit=task_limit)
    hidden_oracles = load_hidden_oracles(base, oracle_path)
    public_leakage_errors = validate_public_tasks_do_not_reference_hidden(base, tasks_path)
    policy = oracle_policy_config(
        oracle_policy=oracle_policy,
        visual_sample_rate=visual_sample_rate,
        visual_on_semantic_failure=visual_on_semantic_failure,
    )
    cascade = cascade_config or cascade_policy_config()

    effective_model = model
    effective_base_url = base_url
    effective_api_key_env = api_key_env
    effective_headers: dict[str, str] | None = None
    if agent_kind == "llm":
        effective_model, provider = resolve_model(
            model, default_base_url=base_url, default_api_key_env=api_key_env
        )
        effective_base_url = provider.base_url
        effective_api_key_env = provider.api_key_env
        effective_headers = provider.extra_headers
        if not os.environ.get(effective_api_key_env):
            raise SynthesisError(f"{effective_api_key_env} is required for evo-bench --agent llm.")
    agent_label = "llm_explore" if (agent_kind == "llm" and explore) else agent_kind
    agent = build_synthesis_agent(
        agent_kind,
        model=effective_model,
        base_url=effective_base_url,
        api_key_env=effective_api_key_env,
        temperature=temperature,
        max_tokens=max_tokens,
        explore=explore,
        max_steps=max_explore_steps,
        extra_headers=effective_headers,
    )
    fallback_seed_agent = None
    if agent_kind == "llm" and explore:
        fallback_seed_agent = build_synthesis_agent(
            agent_kind,
            model=effective_model,
            base_url=effective_base_url,
            api_key_env=effective_api_key_env,
            temperature=temperature,
            max_tokens=max_tokens,
            explore=False,
            max_steps=max_explore_steps,
            extra_headers=effective_headers,
        )

    progress_signature = _evo_progress_signature(
        base=base,
        task_ids=[task.id for task in selected_tasks],
        tasks_path=tasks_path,
        oracle_path=oracle_path,
        agent_kind=agent_kind,
        model=effective_model,
        base_url=effective_base_url,
        api_key_env=effective_api_key_env,
        temperature=temperature,
        max_tokens=max_tokens,
        iterations=iterations,
        population=population,
        memory_mode=memory_mode,
        memory_backend=memory_backend,
        memory_scope=memory_scope,
        memory_in=memory_in,
        memory_out=memory_out,
        memory_policy=memory_policy,
        mutation_policy=mutation_policy,
        repository_memory_writes=repository_memory_writes,
        memory_export=memory_export,
        task_scope=task_scope,
        headless=headless,
        explore=explore,
        max_explore_steps=max_explore_steps,
        provider_retries=provider_retries,
        repair=repair,
        repair_max_attempts=repair_max_attempts,
        diagnosis_enabled=diagnosis_enabled,
        fail_on_provider_outage=fail_on_provider_outage,
        oracle_policy=policy,
        cascade_config=cascade,
    )
    progress_path = out_dir / _EVO_PROGRESS_FILE
    if progress_path.exists() and not resume:
        raise ValueError(
            f"Evo phase already has a progress checkpoint: {progress_path}; pass resume=True or use a new report directory"
        )
    restored_results = (
        _load_evo_progress(
            progress_path,
            signature=progress_signature,
            selected_tasks=selected_tasks,
        )
        if resume and progress_path.exists()
        else []
    )

    initial_memory = load_evo_memory(memory_in) if memory_in is not None else []
    repository_memory: list[MemoryEntry] = list(initial_memory)
    all_memory_entries: list[MemoryEntry] = []
    memory_backend_impl = get_memory_backend(memory_backend, memory_scope=memory_scope)
    repair_config = RepairConfig(
        enabled=repair and agent_kind == "llm" and bool(os.environ.get(effective_api_key_env)),
        model=effective_model,
        base_url=effective_base_url,
        api_key_env=effective_api_key_env,
        extra_headers=effective_headers,
        temperature=temperature,
        max_tokens=max_tokens,
        max_attempts=repair_max_attempts,
    )
    results: list[dict[str, Any]] = list(restored_results)

    def ingest_memory_events(result: dict[str, Any]) -> None:
        for entry in result.get("memory_events", []):
            if not isinstance(entry, dict):
                continue
            memory_entry = _memory_entry_from_dict(entry)
            if memory_export:
                all_memory_entries.append(memory_entry)
            if memory_mode == "repository" and repository_memory_writes:
                repository_memory.append(memory_entry)

    for restored in results:
        ingest_memory_events(restored)

    def write_progress(*, complete: bool) -> None:
        _atomic_write_json(
            progress_path,
            {
                "schema_version": 1,
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "experiment": "evo_synthesis_task_progress",
                "complete": complete,
                "signature": progress_signature,
                "completed_task_ids": [str(result["task_id"]) for result in results],
                "results": results,
            },
        )

    if not progress_path.exists():
        write_progress(complete=False)
    started = time.perf_counter()
    for task in selected_tasks[len(results):]:
        result = _run_evo_task(
            base,
            task,
            hidden_oracles.get(task.id),
            agent,
            out_dir,
            iterations=iterations,
            population=population,
            memory_mode=memory_mode,
            memory_backend=memory_backend_impl,
            repository_memory=repository_memory,
            headless=headless,
            oracle_policy=policy,
            cascade_config=cascade,
            provider_retries=provider_retries,
            repair_config=repair_config,
            fallback_seed_agent=fallback_seed_agent,
            memory_policy=memory_policy,
            mutation_policy=mutation_policy,
            diagnosis_enabled=diagnosis_enabled,
            fail_on_provider_outage=fail_on_provider_outage,
            model=model,
        )
        ingest_memory_events(result)
        results.append(result)
        write_progress(complete=False)

    memory_payload = {
        "schema_version": 2,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "memory_mode": memory_mode,
        "memory_backend": memory_backend,
        "memory_scope": memory_backend_impl.effective_scope,
        "memory_scope_requested": memory_backend_impl.requested_scope,
        "memory_scope_effective": memory_backend_impl.effective_scope,
        "memory_scope_honored": memory_backend_impl.scope_honored,
        "memory_scope_note": memory_backend_impl.scope_warning,
        "memory_policy": memory_policy,
        "input_entries": [entry.to_dict() for entry in initial_memory],
        "input_memory_inventory": memory_backend_impl.inventory(initial_memory),
        "entries": [entry.to_dict() for entry in all_memory_entries],
        "claim_boundary": "Memory contains only public execution feedback and source evidence, never hidden grading outcomes.",
    }
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "experiment": "evo_synthesis",
        "phase": "phase3_all_core" if task_scope == "all-core" else "phase1_browser_web_mvp",
        "synthesis_agent": agent_label,
        "synthesis_model": effective_model if agent_kind == "llm" else None,
        "exploration": explore,
        "max_explore_steps": max_explore_steps if explore else None,
        "iterations": iterations,
        "population": population,
        "task_scope": task_scope,
        "memory_mode": memory_mode,
        "memory_backend": memory_backend,
        "memory_scope": memory_backend_impl.effective_scope,
        "memory_scope_requested": memory_backend_impl.requested_scope,
        "memory_scope_effective": memory_backend_impl.effective_scope,
        "memory_scope_honored": memory_backend_impl.scope_honored,
        "memory_scope_note": memory_backend_impl.scope_warning,
        "memory_policy": memory_policy,
        "mutation_policy": mutation_policy,
        "diagnosis_enabled": diagnosis_enabled,
        "memory_export": memory_export,
        "repository_memory_writes": repository_memory_writes,
        "resume": resume,
        "fail_on_provider_outage": fail_on_provider_outage,
        "resumed_task_count": len(restored_results),
        "checkpoint_path": str(progress_path),
        "memory_in": str(memory_in) if memory_in is not None else None,
        "memory_out": str(memory_out) if memory_out is not None else None,
        "benchmarks": list(benchmarks or (CORE_BENCHMARKS if task_scope == "all-core" else BROWSER_WEB_BENCHMARKS)),
        "oracle_policy": policy.policy,
        "provider_retries": provider_retries,
        "repair_enabled": repair_config.enabled,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "claim_boundary": "Evolution uses public executable feedback only; hidden targets and oracles are evaluator-only after final selection.",
        "public_task_file": str((base / tasks_path).resolve() if not Path(tasks_path).is_absolute() else tasks_path),
        "hidden_oracle_file": str((base / oracle_path).resolve() if not Path(oracle_path).is_absolute() else oracle_path),
        "public_leakage_errors": public_leakage_errors,
        "results": results,
        "summary": summarize_evo_results(results, public_leakage_errors),
    }
    _atomic_write_json(out_dir / "evo-synthesis.json", payload)
    _atomic_write_json(out_dir / "evo-memory.json", memory_payload)
    markdown = render_evo_markdown(payload)
    (out_dir / "evo-synthesis.md").write_text(markdown, encoding="utf-8")
    if set(payload["benchmarks"]).issubset(set(BROWSER_WEB_BENCHMARKS)):
        _atomic_write_json(out_dir / "phase1-browser-web-live.json", payload)
        (out_dir / "phase1-browser-web-live.md").write_text(markdown, encoding="utf-8")
    if memory_out is not None:
        _atomic_write_json(Path(memory_out).expanduser().resolve(), memory_payload)
    write_progress(complete=True)
    return {**payload, "report_dir": str(out_dir), "memory": memory_payload}


def run_phase1_suite(
    root: str | Path = Path.cwd(),
    *,
    include_repository_memory: bool = False,
    report_dir: Path | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    out_dir = report_dir or new_report_dir()
    variants = [
        {
            "name": "source_exploration_seed_only",
            "iterations": 1,
            "population": 1,
            "memory_mode": "none",
            "repair": False,
        },
        {
            "name": "evolution_no_memory",
            "iterations": kwargs.get("iterations", 2),
            "population": kwargs.get("population", 2),
            "memory_mode": "none",
            "repair": kwargs.get("repair", True),
        },
        {
            "name": "evolution_episode_memory",
            "iterations": kwargs.get("iterations", 2),
            "population": kwargs.get("population", 2),
            "memory_mode": "episode",
            "repair": kwargs.get("repair", True),
        },
    ]
    if include_repository_memory:
        variants.append(
            {
                "name": "evolution_repository_memory",
                "iterations": kwargs.get("iterations", 2),
                "population": kwargs.get("population", 2),
                "memory_mode": "repository",
                "repair": kwargs.get("repair", True),
            }
        )

    suite_results: list[dict[str, Any]] = []
    started = time.perf_counter()
    external_memory_out = kwargs.get("memory_out")
    for variant in variants:
        run_kwargs = dict(kwargs)
        run_kwargs.pop("memory_out", None)
        name = str(variant["name"])
        run_kwargs.update({key: value for key, value in variant.items() if key != "name"})
        run_kwargs.setdefault("benchmarks", BROWSER_WEB_BENCHMARKS)
        result = run_evo_synthesis_benchmark(
            root,
            report_dir=out_dir / name,
            **run_kwargs,
        )
        suite_results.append(
            {
                "name": name,
                "report_dir": result["report_dir"],
                "summary": result["summary"],
                "duration_ms": result.get("duration_ms"),
            }
        )

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "evo_phase1_browser_web_suite",
        "benchmarks": list(kwargs.get("benchmarks") or BROWSER_WEB_BENCHMARKS),
        "include_repository_memory": include_repository_memory,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "variants": suite_results,
        "summary": summarize_phase1_suite(suite_results),
    }
    write_json(out_dir / "phase1-browser-web-live.json", payload)
    (out_dir / "phase1-browser-web-live.md").write_text(render_phase1_suite_markdown(payload), encoding="utf-8")
    if external_memory_out:
        write_json(Path(external_memory_out).expanduser().resolve(), payload)
    return {**payload, "report_dir": str(out_dir)}


def run_phase3_suite(
    root: str | Path = Path.cwd(),
    *,
    split_path: str | Path = DEFAULT_PHASE3_SPLIT,
    report_dir: Path | None = None,
    run_meta: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    out_dir = report_dir or new_report_dir()
    split = load_evo_split(split_path)
    suite_results: list[dict[str, Any]] = []
    started = time.perf_counter()
    common_kwargs = dict(kwargs)
    common_kwargs.pop("memory_in", None)
    common_kwargs.pop("memory_out", None)
    common_kwargs.setdefault("task_scope", "all-core")
    common_kwargs.setdefault("benchmarks", CORE_BENCHMARKS)

    for variant in _phase3_variants(common_kwargs):
        variant_id = str(variant["id"])
        variant_kwargs = {key: value for key, value in variant.items() if key not in {"id", "name", "uses_repository_memory"}}
        run_kwargs = {**common_kwargs, **variant_kwargs}
        uses_memory = bool(variant.get("uses_repository_memory"))
        variant_dir = out_dir / variant_id
        train_memory_path = variant_dir / "frozen-train-memory.json"
        train = run_evo_synthesis_benchmark(
            root,
            report_dir=variant_dir / "train",
            task_ids=split.train,
            memory_out=train_memory_path if uses_memory else None,
            memory_export=True,
            repository_memory_writes=True,
            **run_kwargs,
        )
        heldout = run_evo_synthesis_benchmark(
            root,
            report_dir=variant_dir / "heldout",
            task_ids=split.heldout,
            memory_in=train_memory_path if uses_memory else None,
            memory_export=False,
            repository_memory_writes=False,
            **run_kwargs,
        )
        suite_results.append(
            {
                "id": variant_id,
                "name": variant["name"],
                "uses_repository_memory": uses_memory,
                "train_report_dir": train["report_dir"],
                "heldout_report_dir": heldout["report_dir"],
                "frozen_train_memory": str(train_memory_path) if uses_memory else None,
                "train_summary": train["summary"],
                "heldout_summary": heldout["summary"],
            }
        )

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "evo_phase3_core_suite",
        "split": {"name": split.name, "path": str(split_path), "train_task_count": len(split.train), "heldout_task_count": len(split.heldout)},
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "variants": suite_results,
        "summary": summarize_phase3_suite(suite_results),
        "claim_boundary": "Train memory is public-only; held-out runs read frozen train memory and do not write repository memory.",
    }
    run_context.stamp_payload(payload, run_meta=run_meta, experiment_id="evo_phase3_core_suite", git_root=root)
    write_json(out_dir / "phase3-core-suite.json", payload)
    (out_dir / "phase3-core-suite.md").write_text(render_phase3_suite_markdown(payload), encoding="utf-8")
    return {**payload, "report_dir": str(out_dir)}


DEFAULT_MEMORY_BASELINE_BACKENDS = ("amem", "mem0", "stateportal-anchor")


def _load_resumable_evo_phase(
    report_dir: Path,
    *,
    expected_task_ids: Sequence[str],
    backend: str,
    memory_in: Path | None,
    memory_out: Path | None,
    memory_export: bool,
    repository_memory_writes: bool,
) -> dict[str, Any] | None:
    """Return a complete, provenance-compatible phase report for C2 resume.

    Interrupted phases never write ``evo-synthesis.json`` because that report is
    emitted only after every task completes.  Reusing only this final artifact
    prevents a partial task directory from silently becoming experiment evidence.
    """

    report_path = report_dir / "evo-synthesis.json"
    memory_path = report_dir / "evo-memory.json"
    if not report_path.is_file() or not memory_path.is_file():
        return None
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        memory = json.loads(memory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not isinstance(memory, dict):
        return None
    if payload.get("experiment") != "evo_synthesis":
        return None
    if payload.get("memory_mode") != "repository" or payload.get("memory_backend") != backend:
        return None
    if payload.get("memory_export") is not memory_export:
        return None
    if payload.get("repository_memory_writes") is not repository_memory_writes:
        return None
    results = payload.get("results")
    if not isinstance(results, list):
        return None
    task_ids = [str(row.get("task_id")) for row in results if isinstance(row, dict)]
    if task_ids != list(expected_task_ids):
        return None

    def same_path(value: object, expected: Path | None) -> bool:
        if expected is None:
            return value is None
        if not isinstance(value, str) or not value:
            return False
        return Path(value).expanduser().resolve() == expected.expanduser().resolve()

    if not same_path(payload.get("memory_in"), memory_in):
        return None
    if not same_path(payload.get("memory_out"), memory_out):
        return None
    if memory_out is not None and not memory_out.is_file():
        return None
    if not isinstance(payload.get("summary"), dict):
        return None
    return {**payload, "memory": memory, "report_dir": str(report_dir)}


def _required_c2_summary_count(
    report: dict[str, Any],
    key: str,
    *,
    backend: str,
    phase: str,
) -> int:
    summary = report.get("summary")
    value = summary.get(key) if isinstance(summary, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/{phase}: "
            f"summary.{key} must be a non-negative integer; use a new report "
            "directory after correcting the synthesis path"
        )
    return value


def _validate_c2_phase_evidence(
    report: dict[str, Any],
    *,
    backend: str,
    phase: str,
    expected_task_ids: Sequence[str],
) -> dict[str, Any]:
    """Fail closed when a completed C2 phase contains no model evaluation.

    This deliberately does not require a strict/public success or a runtime
    execution.  A model may legitimately produce only admission-rejected
    candidates; the invalid condition is a phase that evaluated no candidate at
    all, which cannot support an effect conclusion.
    """

    expected_ids = list(expected_task_ids)
    total_tasks = _required_c2_summary_count(
        report, "total_tasks", backend=backend, phase=phase
    )
    results = report.get("results")
    actual_ids = (
        [str(row.get("task_id")) for row in results if isinstance(row, dict)]
        if isinstance(results, list)
        else []
    )
    if total_tasks != len(expected_ids) or actual_ids != expected_ids:
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/{phase}: completed "
            f"task coverage does not match the split ({total_tasks} summary tasks, "
            f"{len(actual_ids)} result tasks, {len(expected_ids)} expected); use a "
            "new report directory after correcting the synthesis path"
        )
    candidate_evaluations = _required_c2_summary_count(
        report, "candidate_evaluations", backend=backend, phase=phase
    )
    if candidate_evaluations == 0:
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/{phase}: no candidate "
            "was evaluated, so the phase has no effect evidence; use a new report "
            "directory after correcting the synthesis path"
        )
    strict_success = _required_c2_summary_count(
        report, "strict_success", backend=backend, phase=phase
    )
    if strict_success > total_tasks:
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/{phase}: "
            f"summary.strict_success={strict_success} exceeds total_tasks={total_tasks}; "
            "use a new report directory after correcting the synthesis path"
        )
    summary = report["summary"]
    return {
        "expected_tasks": len(expected_ids),
        "total_tasks": total_tasks,
        "candidate_evaluations": candidate_evaluations,
        "candidates_executed": summary.get("candidates_executed"),
        "strict_success": strict_success,
    }


def _memory_transfer_sha256(entries: Sequence[dict[str, Any]]) -> str:
    """Hash treatment content while excluding mutable retrieval scores."""

    stable_entries = [
        {key: value for key, value in entry.items() if key != "score"}
        for entry in entries
    ]
    encoded = json.dumps(
        stable_entries,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_frozen_train_memory(
    path: Path,
    *,
    backend: str,
) -> dict[str, Any]:
    """Audit the exact frozen file that held-out is expected to consume."""

    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/train: frozen train "
            "memory is missing or unreadable; use a new report directory after "
            "correcting the synthesis path"
        ) from error
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries or not all(
        isinstance(entry, dict) for entry in entries
    ):
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/train: frozen train "
            "memory contains no usable entries, so no memory transfer can be "
            "compared; use a new report directory after correcting the synthesis path"
        )
    return {
        "entries": entries,
        "entry_count": len(entries),
        "file_sha256": hashlib.sha256(raw).hexdigest(),
        "transfer_sha256": _memory_transfer_sha256(entries),
    }


def _validate_heldout_memory_inventory(
    report: dict[str, Any],
    *,
    backend: str,
    frozen_memory: dict[str, Any],
) -> dict[str, Any]:
    memory = report.get("memory")
    inventory = (
        memory.get("input_memory_inventory") if isinstance(memory, dict) else None
    )
    input_entries = (
        inventory.get("total_entries") if isinstance(inventory, dict) else None
    )
    frozen_entry_count = frozen_memory["entry_count"]
    if (
        isinstance(input_entries, bool)
        or not isinstance(input_entries, int)
        or input_entries <= 0
        or input_entries != frozen_entry_count
    ):
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/heldout: input memory "
            f"inventory has {input_entries!r} entries but the frozen train file has "
            f"{frozen_entry_count}; use a new report directory after correcting the "
            "synthesis path"
        )
    actual_entries = memory.get("input_entries") if isinstance(memory, dict) else None
    if not isinstance(actual_entries, list) or not all(
        isinstance(entry, dict) for entry in actual_entries
    ):
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/heldout: actual input "
            "memory entries are missing or malformed; use a new report directory "
            "after correcting the synthesis path"
        )
    input_transfer_sha256 = _memory_transfer_sha256(actual_entries)
    if (
        len(actual_entries) != frozen_entry_count
        or input_transfer_sha256 != frozen_memory["transfer_sha256"]
    ):
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/heldout: actual input "
            "memory content does not match frozen train memory; use a new report "
            "directory after correcting the synthesis path"
        )
    return {
        "input_memory_entries": input_entries,
        "input_memory_transfer_sha256": input_transfer_sha256,
    }


def _validate_train_memory_export(
    report: dict[str, Any],
    *,
    backend: str,
    frozen_memory: dict[str, Any],
) -> str:
    """Prove the frozen treatment is the memory emitted by the train phase."""

    memory = report.get("memory")
    entries = memory.get("entries") if isinstance(memory, dict) else None
    if not isinstance(entries, list) or not entries or not all(
        isinstance(entry, dict) for entry in entries
    ):
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/train: exported memory "
            "entries are missing or malformed; use a new report directory after "
            "correcting the synthesis path"
        )
    export_sha256 = _memory_transfer_sha256(entries)
    if export_sha256 != frozen_memory["transfer_sha256"]:
        raise MemoryBaselineEvidenceInvalidError(
            f"C2 memory evidence is invalid for {backend}/train: frozen memory "
            "content does not match the train phase export; use a new report "
            "directory after correcting the synthesis path"
        )
    return export_sha256


def run_memory_baseline_suite(
    root: str | Path = Path.cwd(),
    *,
    split_path: str | Path = DEFAULT_PHASE3_SPLIT,
    backends: Sequence[str] = DEFAULT_MEMORY_BASELINE_BACKENDS,
    anchor_backend: str = "stateportal-anchor",
    report_dir: Path | None = None,
    run_meta: dict[str, Any] | None = None,
    resume: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Compare StatePortal anchor vs A-MEM and Mem0 under one transfer protocol.

    Each backend runs the same frozen-train -> held-out pipeline (per-game train
    builds public-only frozen memory; held-out reads it via the backend) under a
    fixed memory arm so the only difference is the memory system. The report
    aggregates held-out strict success overall and per benchmark, plus the anchor
    delta against each baseline. With the deterministic agent there is no learning
    gain by design; this validates the plumbing and the comparison structure.
    """
    out_dir = report_dir or new_report_dir()
    split = load_evo_split(split_path)
    for name in backends:
        if name not in MEMORY_BACKEND_CHOICES:
            raise ValueError(f"unknown memory backend {name!r}; choices: {MEMORY_BACKEND_CHOICES}")
    if anchor_backend not in backends:
        raise ValueError(
            f"anchor_backend {anchor_backend!r} must be included in backends {list(backends)!r}; "
            "the suite computes anchor-vs-baseline deltas and cannot report them without running the anchor."
        )

    common = dict(kwargs)
    for drop in ("memory_in", "memory_out", "memory_backend", "memory_scope", "task_ids", "memory_export", "repository_memory_writes"):
        common.pop(drop, None)
    common.setdefault("task_scope", "all-core")
    common.setdefault("benchmarks", CORE_BENCHMARKS)
    # The suite is defined by frozen-train -> held-out repository-memory transfer,
    # so force the repository arm regardless of any inherited --memory-mode default.
    common["memory_mode"] = "repository"
    common.setdefault("memory_policy", "positive-negative")
    common.setdefault("mutation_policy", "source-constrained")
    # A formal backend comparison cannot interpret provider infrastructure or
    # unauditable usage as a model failure.  Leave the atomic task checkpoint
    # resumable, but propagate either condition so P5 records the suite as
    # incomplete rather than scoring an empty population.
    common["fail_on_provider_outage"] = True
    # Memory only enters repair/replay from generation >= 1, so held-out needs >= 2 iterations.
    iterations = int(common.get("iterations", 2) or 2)
    iteration_adjusted = iterations < 2
    common["iterations"] = max(2, iterations)

    started = time.perf_counter()
    suite_results: list[dict[str, Any]] = []
    validity_checks: list[dict[str, Any]] = []
    for backend in backends:
        # Request each backend's native scope so no spurious not-honored warnings
        # appear: StatePortal is intra-only, the others are game-agnostic (both).
        requested_scope = "intra" if backend == "stateportal" else "both"
        backend_dir = out_dir / backend
        train_memory_path = backend_dir / "frozen-train-memory.json"
        train_dir = backend_dir / "train"
        heldout_dir = backend_dir / "heldout"
        train = (
            _load_resumable_evo_phase(
                train_dir,
                expected_task_ids=split.train,
                backend=backend,
                memory_in=None,
                memory_out=train_memory_path,
                memory_export=True,
                repository_memory_writes=True,
            )
            if resume
            else None
        )
        train_resumed = train is not None
        if train is None:
            train = run_evo_synthesis_benchmark(
                root,
                report_dir=train_dir,
                task_ids=split.train,
                memory_backend=backend,
                memory_scope=requested_scope,
                memory_out=train_memory_path,
                memory_export=True,
                repository_memory_writes=True,
                resume=resume,
                **common,
            )
        train_validity = _validate_c2_phase_evidence(
            train,
            backend=backend,
            phase="train",
            expected_task_ids=split.train,
        )
        frozen_memory = _validate_frozen_train_memory(
            train_memory_path,
            backend=backend,
        )
        train_validity.update(
            {
                "frozen_memory_entries": frozen_memory["entry_count"],
                "frozen_memory_file_sha256": frozen_memory["file_sha256"],
                "frozen_memory_transfer_sha256": frozen_memory["transfer_sha256"],
                "train_memory_export_sha256": _validate_train_memory_export(
                    train,
                    backend=backend,
                    frozen_memory=frozen_memory,
                ),
            }
        )
        heldout = (
            _load_resumable_evo_phase(
                heldout_dir,
                expected_task_ids=split.heldout,
                backend=backend,
                memory_in=train_memory_path,
                memory_out=None,
                memory_export=False,
                repository_memory_writes=False,
            )
            if resume
            else None
        )
        heldout_resumed = heldout is not None
        if heldout is None:
            heldout = run_evo_synthesis_benchmark(
                root,
                report_dir=heldout_dir,
                task_ids=split.heldout,
                memory_backend=backend,
                memory_scope=requested_scope,
                memory_in=train_memory_path,
                memory_export=False,
                repository_memory_writes=False,
                resume=resume,
                **common,
            )
        heldout_validity = _validate_c2_phase_evidence(
            heldout,
            backend=backend,
            phase="heldout",
            expected_task_ids=split.heldout,
        )
        heldout_validity.update(
            _validate_heldout_memory_inventory(
                heldout,
                backend=backend,
                frozen_memory=frozen_memory,
            )
        )
        validity_checks.append(
            {
                "backend": backend,
                "train": train_validity,
                "heldout": heldout_validity,
            }
        )
        suite_results.append(
            {
                "backend": backend,
                "memory_scope_requested": requested_scope,
                "memory_scope": heldout.get("memory_scope_effective", requested_scope),
                "memory_scope_effective": heldout.get("memory_scope_effective", requested_scope),
                "memory_scope_honored": heldout.get("memory_scope_honored", True),
                "is_anchor": backend == anchor_backend,
                "train_report_dir": train["report_dir"],
                "heldout_report_dir": heldout["report_dir"],
                "frozen_train_memory": str(train_memory_path),
                "train_resumed": train_resumed,
                "heldout_resumed": heldout_resumed,
                "frozen_memory_inventory": heldout.get("memory", {}).get("input_memory_inventory", {}),
                "train_summary": train["summary"],
                "heldout_summary": heldout["summary"],
            }
        )

    summary = summarize_memory_baseline_suite(suite_results, anchor_backend=anchor_backend)
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "experiment": "memory_baseline_transfer_suite",
        "execution_seed": run_meta.get("seed") if isinstance(run_meta, dict) else None,
        "anchor_backend": anchor_backend,
        "baseline_backends": [b for b in backends if b != anchor_backend],
        "split": {"name": split.name, "path": str(split_path), "train_task_count": len(split.train), "heldout_task_count": len(split.heldout)},
        "iterations_adjusted_to_min_two": iteration_adjusted,
        "resume": resume,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "variants": suite_results,
        "formal_validity": {
            "status": "valid",
            "rules": [
                "phase_task_count_matches_split",
                "each_phase_has_candidate_evaluations",
                "frozen_train_memory_is_nonempty",
                "frozen_train_memory_matches_train_export",
                "heldout_input_inventory_matches_frozen_train_memory",
                "heldout_input_content_matches_frozen_train_memory",
            ],
            "backend_checks": validity_checks,
        },
        "summary": summary,
        "claim_boundary": "All backends read public-only frozen train memory; held-out runs do not write frozen repository memory. Anchor improvements never read hidden grading material.",
    }
    run_context.stamp_payload(payload, run_meta=run_meta, experiment_id="memory_baseline_transfer_suite", git_root=root)
    write_json(out_dir / "memory-baseline-suite.json", payload)
    (out_dir / "memory-baseline-suite.md").write_text(render_memory_baseline_suite_markdown(payload), encoding="utf-8")
    return {**payload, "report_dir": str(out_dir)}


def summarize_memory_baseline_suite(variants: Sequence[dict[str, Any]], *, anchor_backend: str) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    strict_by_backend: dict[str, int] = {}
    anchor_present = any(str(v.get("backend")) == anchor_backend for v in variants)
    anchor_strict = 0
    for variant in variants:
        heldout = variant.get("heldout_summary") if isinstance(variant.get("heldout_summary"), dict) else {}
        backend = str(variant.get("backend"))
        strict = int(heldout.get("strict_success", 0) or 0)
        strict_by_backend[backend] = strict
        if backend == anchor_backend:
            anchor_strict = strict
        rows.append(
            {
                "backend": backend,
                "is_anchor": variant.get("is_anchor", False),
                "heldout_tasks": int(heldout.get("total_tasks", 0) or 0),
                "heldout_strict_success": strict,
                "heldout_public_success": int(heldout.get("public_validation_success", 0) or 0),
                "heldout_scaffold_success": int(heldout.get("scaffold_success", 0) or 0),
                "heldout_strict_scaffold_gap": int(heldout.get("strict_scaffold_gap", 0) or 0),
                "invalid_checkpoint_rate": heldout.get("invalid_checkpoint_rate", 0.0),
                "strict_by_benchmark": {b: v.get("strict_success", 0) for b, v in (heldout.get("by_benchmark", {}) or {}).items()},
            }
        )
    # Only compute anchor deltas when the anchor was actually run; otherwise the
    # comparison would be against a phantom anchor with strict success 0.
    if not anchor_present:
        return {
            "anchor_backend": anchor_backend,
            "anchor_present": False,
            "anchor_heldout_strict_success": None,
            "anchor_delta_vs_baseline": {},
            "anchor_beats_all_baselines": None,
            "anchor_at_least_matches_all_baselines": None,
            "rows": rows,
        }
    for row in rows:
        if not row["is_anchor"]:
            row["anchor_minus_this"] = anchor_strict - row["heldout_strict_success"]
    anchor_deltas = {b: anchor_strict - s for b, s in strict_by_backend.items() if b != anchor_backend}
    return {
        "anchor_backend": anchor_backend,
        "anchor_present": True,
        "anchor_heldout_strict_success": anchor_strict,
        "anchor_delta_vs_baseline": anchor_deltas,
        "anchor_beats_all_baselines": all(delta > 0 for delta in anchor_deltas.values()) if anchor_deltas else False,
        "anchor_at_least_matches_all_baselines": all(delta >= 0 for delta in anchor_deltas.values()) if anchor_deltas else False,
        "rows": rows,
    }


def render_memory_baseline_suite_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# StatePortal Memory Baseline Transfer Suite",
        "",
        f"Anchor backend: `{payload.get('anchor_backend')}` | Baselines: "
        + ", ".join(f"`{b}`" for b in payload.get("baseline_backends", [])),
        "",
        "Held-out strict checkpoint-synthesis success per memory backend (same frozen-train -> held-out protocol, same memory arm).",
        "",
        "| Backend | Anchor | Held-out tasks | Strict | Public | Scaffold | Strict-scaffold gap | Anchor - this |",
        "|---|:--:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in summary.get("rows", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("backend")),
                    "yes" if row.get("is_anchor") else "",
                    str(row.get("heldout_tasks")),
                    str(row.get("heldout_strict_success")),
                    str(row.get("heldout_public_success")),
                    str(row.get("heldout_scaffold_success")),
                    str(row.get("heldout_strict_scaffold_gap")),
                    "" if row.get("is_anchor") else str(row.get("anchor_minus_this")),
                ]
            )
            + " |"
        )
    deltas = summary.get("anchor_delta_vs_baseline", {})
    lines.extend(
        [
            "",
            f"Anchor held-out strict success: {summary.get('anchor_heldout_strict_success')}",
            "Anchor delta vs each baseline: " + (", ".join(f"{b}: {d:+d}" for b, d in deltas.items()) if deltas else "n/a"),
            f"Anchor beats all baselines: {summary.get('anchor_beats_all_baselines')}",
            "",
            "_Note: with the deterministic agent there is no learning gain by design; this run validates plumbing and the comparison structure. Real transfer numbers require running with `--agent llm` and a configured model._",
        ]
    )
    return "\n".join(lines) + "\n"


def _phase3_variants(common_kwargs: dict[str, Any]) -> list[dict[str, Any]]:
    base_iterations = int(common_kwargs.get("iterations", 2) or 2)
    base_population = int(common_kwargs.get("population", 2) or 2)
    return [
        {"id": "A1", "name": "single_shot_no_explore", "iterations": 1, "population": 1, "explore": False, "repair": False, "memory_mode": "none", "mutation_policy": "none", "memory_policy": "positive-negative", "diagnosis_enabled": True, "uses_repository_memory": False},
        {"id": "A2", "name": "source_exploration_seed_only", "iterations": 1, "population": 1, "explore": True, "repair": False, "memory_mode": "none", "mutation_policy": "none", "memory_policy": "positive-negative", "diagnosis_enabled": True, "uses_repository_memory": False},
        {"id": "A3", "name": "self_refine_no_memory", "iterations": base_iterations, "population": 1, "repair": True, "memory_mode": "none", "mutation_policy": "none", "memory_policy": "positive-negative", "diagnosis_enabled": True, "uses_repository_memory": False},
        {"id": "A4", "name": "random_mutation", "iterations": base_iterations, "population": base_population, "repair": False, "memory_mode": "none", "mutation_policy": "random", "memory_policy": "positive-negative", "diagnosis_enabled": True, "uses_repository_memory": False},
        {"id": "A5", "name": "evolution_no_memory", "iterations": base_iterations, "population": base_population, "repair": True, "memory_mode": "none", "mutation_policy": "source-constrained", "memory_policy": "positive-negative", "diagnosis_enabled": True, "uses_repository_memory": False},
        {"id": "A6", "name": "textual_memory", "iterations": base_iterations, "population": base_population, "repair": True, "memory_mode": "repository", "mutation_policy": "source-constrained", "memory_policy": "textual-only", "diagnosis_enabled": True, "uses_repository_memory": True},
        {"id": "A7", "name": "structured_positive_memory", "iterations": base_iterations, "population": base_population, "repair": True, "memory_mode": "repository", "mutation_policy": "source-constrained", "memory_policy": "positive-only", "diagnosis_enabled": True, "uses_repository_memory": True},
        {"id": "A8", "name": "positive_negative_memory_no_diagnosis", "iterations": base_iterations, "population": base_population, "repair": True, "memory_mode": "repository", "mutation_policy": "source-constrained", "memory_policy": "positive-negative", "diagnosis_enabled": False, "uses_repository_memory": True},
        {"id": "A9", "name": "full_evoportal", "iterations": base_iterations, "population": base_population, "repair": True, "memory_mode": "repository", "mutation_policy": "source-constrained", "memory_policy": "positive-negative", "diagnosis_enabled": True, "uses_repository_memory": True},
    ]


def summarize_phase1_suite(variants: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for variant in variants:
        summary = variant.get("summary") if isinstance(variant.get("summary"), dict) else {}
        total = int(summary.get("total_tasks", 0) or 0)
        rows.append(
            {
                "name": variant.get("name"),
                "total_tasks": total,
                "public_validation_success": summary.get("public_validation_success", 0),
                "strict_success": summary.get("strict_success", 0),
                "scaffold_success": summary.get("scaffold_success", 0),
                "schema_rejected": summary.get("schema_rejected", 0),
                "scaffold_rejected": summary.get("scaffold_rejected", 0),
                "provider_failures": summary.get("provider_failures", 0),
                "fallback_retries": summary.get("fallback_retries", 0),
                "candidate_evaluations": summary.get("candidate_evaluations", 0),
                "candidates_executed": summary.get("candidates_executed", 0),
                "duration_ms": variant.get("duration_ms"),
            }
        )
    return {"variants": rows}


def render_phase1_suite_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# EvoPortal Phase 1 Browser/Web Suite",
        "",
        "| Variant | Tasks | Public valid | Strict | Scaffold | Schema rejected | Provider failures | Executed | Duration ms |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("summary", {}).get("variants", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("name")),
                    str(row.get("total_tasks")),
                    str(row.get("public_validation_success")),
                    str(row.get("strict_success")),
                    str(row.get("scaffold_success")),
                    str(row.get("schema_rejected")),
                    str(row.get("provider_failures")),
                    str(row.get("candidates_executed")),
                    str(row.get("duration_ms")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def summarize_phase3_suite(variants: Sequence[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    baseline_heldout = 0
    for variant in variants:
        if variant.get("id") == "A5":
            heldout_summary = variant.get("heldout_summary") if isinstance(variant.get("heldout_summary"), dict) else {}
            baseline_heldout = int(heldout_summary.get("strict_success", 0) or 0)
            break
    for variant in variants:
        train = variant.get("train_summary") if isinstance(variant.get("train_summary"), dict) else {}
        heldout = variant.get("heldout_summary") if isinstance(variant.get("heldout_summary"), dict) else {}
        heldout_total = int(heldout.get("total_tasks", 0) or 0)
        heldout_strict = int(heldout.get("strict_success", 0) or 0)
        heldout_scaffold = int(heldout.get("scaffold_success", 0) or 0)
        executed = int(heldout.get("candidates_executed", 0) or 0)
        duration_ms = int(heldout.get("duration_ms", 0) or 0)
        rows.append(
            {
                "id": variant.get("id"),
                "name": variant.get("name"),
                "train_tasks": train.get("total_tasks", 0),
                "heldout_tasks": heldout_total,
                "train_strict_success": train.get("strict_success", 0),
                "heldout_strict_success": heldout_strict,
                "heldout_public_success": heldout.get("public_validation_success", 0),
                "heldout_scaffold_success": heldout_scaffold,
                "heldout_strict_scaffold_gap": heldout_scaffold - heldout_strict,
                "transfer_gain_vs_A5": heldout_strict - baseline_heldout,
                "cost_normalized_success_per_execution": round(heldout_strict / max(1, executed), 6),
                "cost_normalized_success_per_minute": round(heldout_strict / max(1.0, duration_ms / 60000.0), 6),
                "invalid_checkpoint_rate": heldout.get("invalid_checkpoint_rate", 0.0),
                "repair_success_rate": heldout.get("repair_success_rate", {}),
                "memory_hit_utility": heldout.get("memory_hit_utility", {}),
                "frozen_train_memory": variant.get("frozen_train_memory"),
            }
        )
    return {"variants": rows}


def render_phase3_suite_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# EvoPortal Phase 3 Core Suite",
        "",
        f"- Split: `{payload.get('split', {}).get('name')}`",
        f"- Train tasks: `{payload.get('split', {}).get('train_task_count')}`",
        f"- Held-out tasks: `{payload.get('split', {}).get('heldout_task_count')}`",
        "",
        "| Variant | Train strict | Held-out strict | Held-out public | Scaffold | Gap | Transfer vs A5 | Invalid rate |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload.get("summary", {}).get("variants", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    f"{row.get('id')} {row.get('name')}",
                    str(row.get("train_strict_success")),
                    str(row.get("heldout_strict_success")),
                    str(row.get("heldout_public_success")),
                    str(row.get("heldout_scaffold_success")),
                    str(row.get("heldout_strict_scaffold_gap")),
                    str(row.get("transfer_gain_vs_A5")),
                    str(row.get("invalid_checkpoint_rate")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def summarize_evo_results(results: Sequence[dict[str, Any]], public_leakage_errors: Sequence[str] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total_tasks": len(results),
        "public_leakage_failures": len(public_leakage_errors or []),
        "strict_success": 0,
        "scaffold_success": 0,
        "public_validation_success": 0,
        "candidate_evaluations": 0,
        "candidates_executed": 0,
        "schema_rejected": 0,
        "scaffold_rejected": 0,
        "provider_failures": 0,
        "fallback_retries": 0,
        "memory_context_tasks": 0,
        "invalid_checkpoint_count": 0,
        "invalid_checkpoint_rate": 0.0,
        "iterations_to_valid": {},
        "repair_success_rate": {},
        "operator_success": {},
        "cost_normalized_success_per_execution": 0.0,
        "cost_normalized_success_per_minute": 0.0,
        "strict_scaffold_gap": 0,
        "diagnoses": {},
        "evolution_curve": {},
        "failure_reduction_curve": {},
        "memory_hit_utility": {"tasks_with_memory": 0, "public_success_with_memory": 0, "strict_success_with_memory": 0},
        "by_benchmark": {},
    }
    for result in results:
        benchmark = str(result.get("benchmark"))
        bucket = summary["by_benchmark"].setdefault(benchmark, {"total": 0, "strict_success": 0, "public_validation_success": 0})
        bucket["total"] += 1
        provider = result.get("provider") if isinstance(result.get("provider"), dict) else {}
        summary["provider_failures"] += int(provider.get("provider_failures", 0) or 0)
        summary["fallback_retries"] += int(provider.get("fallback_retries", 0) or 0)
        memory_context = result.get("memory_context") if isinstance(result.get("memory_context"), list) else []
        generation_contexts = result.get("memory_context_by_generation") if isinstance(result.get("memory_context_by_generation"), list) else []
        has_generation_memory_context = any(
            isinstance(item, dict) and bool(item.get("entries")) for item in generation_contexts
        )
        has_memory_context = bool(memory_context) or has_generation_memory_context
        if has_memory_context:
            summary["memory_context_tasks"] += 1
            summary["memory_hit_utility"]["tasks_with_memory"] += 1
        final_eval = result.get("final_evaluation") if isinstance(result.get("final_evaluation"), dict) else {}
        final_diag = final_eval.get("diagnosis", {}) if isinstance(final_eval.get("diagnosis"), dict) else {}
        if final_diag.get("label") == "success":
            summary["public_validation_success"] += 1
            bucket["public_validation_success"] += 1
            if has_memory_context:
                summary["memory_hit_utility"]["public_success_with_memory"] += 1
        grading = result.get("final_grading") if isinstance(result.get("final_grading"), dict) else {}
        strict = grading.get("strict") if isinstance(grading.get("strict"), dict) else {}
        scaffold = grading.get("scaffold") if isinstance(grading.get("scaffold"), dict) else {}
        if strict.get("status") == "success":
            summary["strict_success"] += 1
            bucket["strict_success"] += 1
            if has_memory_context:
                summary["memory_hit_utility"]["strict_success_with_memory"] += 1
        if scaffold.get("status") == "success":
            summary["scaffold_success"] += 1
        first_valid_generation: int | None = None
        for candidate in result.get("candidates", []):
            if not isinstance(candidate, dict):
                continue
            summary["candidate_evaluations"] += 1
            if candidate.get("executed"):
                summary["candidates_executed"] += 1
            errors = candidate.get("admission_errors") or []
            if errors:
                summary["schema_rejected"] += 1
            if any("scaffold" in str(error) for error in errors):
                summary["scaffold_rejected"] += 1
            label = candidate.get("diagnosis", {}).get("label", "unknown")
            summary["diagnoses"][label] = summary["diagnoses"].get(label, 0) + 1
            if label in {"schema_error", "runtime_error", "soft_locked", "persistence_mismatch"}:
                summary["invalid_checkpoint_count"] += 1
            generation = str(candidate.get("hypothesis", {}).get("generation", "unknown"))
            if label == "success" and first_valid_generation is None and generation.isdigit():
                first_valid_generation = int(generation)
            generation_bucket = summary["evolution_curve"].setdefault(generation, {"total": 0, "executed": 0, "success": 0, "schema_error": 0})
            generation_bucket["total"] += 1
            if candidate.get("executed"):
                generation_bucket["executed"] += 1
            if label == "success":
                generation_bucket["success"] += 1
            if label == "schema_error":
                generation_bucket["schema_error"] += 1
            failure_bucket = summary["failure_reduction_curve"].setdefault(generation, {})
            failure_bucket[label] = failure_bucket.get(label, 0) + 1
            hypothesis = candidate.get("hypothesis") if isinstance(candidate.get("hypothesis"), dict) else {}
            operator_id = str(hypothesis.get("operator_id") or hypothesis.get("mutation") or "unknown")
            if operator_id not in {"seed", "seed_checkpoint"} and not operator_id.startswith("raw_model"):
                op_bucket = summary["operator_success"].setdefault(operator_id, {"attempts": 0, "success": 0})
                op_bucket["attempts"] += 1
                if label == "success":
                    op_bucket["success"] += 1
        summary["iterations_to_valid"][str(result.get("task_id"))] = first_valid_generation
    for operator_id, bucket in summary["operator_success"].items():
        attempts = int(bucket.get("attempts", 0) or 0)
        summary["repair_success_rate"][operator_id] = round(int(bucket.get("success", 0) or 0) / max(1, attempts), 6)
    summary["invalid_checkpoint_rate"] = round(summary["invalid_checkpoint_count"] / max(1, summary["candidate_evaluations"]), 6)
    duration_ms = sum(int(result.get("duration_ms", 0) or 0) for result in results)
    summary["cost_normalized_success_per_execution"] = round(summary["strict_success"] / max(1, summary["candidates_executed"]), 6)
    summary["cost_normalized_success_per_minute"] = round(summary["strict_success"] / max(1.0, duration_ms / 60000.0), 6)
    summary["strict_scaffold_gap"] = summary["scaffold_success"] - summary["strict_success"]
    return summary


def render_evo_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    title = "EvoPortal Phase 3 Core" if payload.get("phase") == "phase3_all_core" else "EvoPortal Phase 1 MVP"
    lines = [
        f"# {title}",
        "",
        f"- Total tasks: `{summary.get('total_tasks', 0)}`",
        f"- Public validation success: `{summary.get('public_validation_success', 0)}`",
        f"- Strict success: `{summary.get('strict_success', 0)}`",
        f"- Scaffold success: `{summary.get('scaffold_success', 0)}`",
        f"- Candidate evaluations: `{summary.get('candidate_evaluations', 0)}`",
        f"- Provider failures: `{summary.get('provider_failures', 0)}`",
        f"- Fallback retries: `{summary.get('fallback_retries', 0)}`",
        "",
        "| Task | Benchmark | Final public diagnosis | Strict | Scaffold | Executed candidates |",
        "|---|---|---|---|---|---|",
    ]
    for result in payload.get("results", []):
        final_eval = result.get("final_evaluation") if isinstance(result.get("final_evaluation"), dict) else {}
        diagnosis = final_eval.get("diagnosis", {}) if isinstance(final_eval.get("diagnosis"), dict) else {}
        grading = result.get("final_grading") if isinstance(result.get("final_grading"), dict) else {}
        strict = grading.get("strict") if isinstance(grading.get("strict"), dict) else {}
        scaffold = grading.get("scaffold") if isinstance(grading.get("scaffold"), dict) else {}
        executed = sum(1 for item in result.get("candidates", []) if isinstance(item, dict) and item.get("executed"))
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result.get("task_id")),
                    str(result.get("benchmark")),
                    str(diagnosis.get("label", "none")),
                    str(strict.get("status", "none")),
                    str(scaffold.get("status", "none")),
                    str(executed),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Evolution Curve", "", "| Generation | Total | Executed | Success | Schema error |", "|---:|---:|---:|---:|---:|"])
    for generation, row in sorted(summary.get("evolution_curve", {}).items(), key=lambda item: str(item[0])):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(generation),
                    str(row.get("total", 0)),
                    str(row.get("executed", 0)),
                    str(row.get("success", 0)),
                    str(row.get("schema_error", 0)),
                ]
            )
            + " |"
        )
    lines.extend(["", "## Failure Reduction", "", "| Generation | Diagnosis counts |", "|---:|---|"])
    for generation, row in sorted(summary.get("failure_reduction_curve", {}).items(), key=lambda item: str(item[0])):
        counts = ", ".join(f"{key}: {value}" for key, value in sorted(row.items()))
        lines.append(f"| {generation} | {counts} |")
    memory_utility = summary.get("memory_hit_utility", {}) if isinstance(summary.get("memory_hit_utility"), dict) else {}
    lines.extend(
        [
            "",
            "## Memory Utility",
            "",
            f"- Tasks with memory context: `{memory_utility.get('tasks_with_memory', 0)}`",
            f"- Public success with memory: `{memory_utility.get('public_success_with_memory', 0)}`",
            f"- Strict success with memory: `{memory_utility.get('strict_success_with_memory', 0)}`",
        ]
    )
    return "\n".join(lines) + "\n"


def _run_evo_task(
    root: Path,
    task: PublicIssue,
    hidden_oracle: Any,
    agent: Any,
    out_dir: Path,
    *,
    iterations: int,
    population: int,
    memory_mode: str,
    memory_backend: MemoryBackend,
    repository_memory: list[MemoryEntry],
    headless: bool,
    oracle_policy: OraclePolicyConfig,
    cascade_config: CascadePolicyConfig,
    provider_retries: int,
    repair_config: RepairConfig,
    fallback_seed_agent: Any | None,
    memory_policy: str,
    mutation_policy: str,
    diagnosis_enabled: bool,
    fail_on_provider_outage: bool,
    model: str,
) -> dict[str, Any]:
    task_started = time.perf_counter()
    # A task that was interrupted before reaching the phase checkpoint must not
    # inherit its old synthesis/runtime artifacts on resume.  Keep every try in
    # an immutable attempt namespace; only the task result in evo-progress.json
    # is eligible for reuse.
    task_root = out_dir / "evo-runs" / task.id
    attempt = 1
    while True:
        task_dir = task_root / f"attempt-{attempt:04d}"
        try:
            task_dir.mkdir(parents=True, exist_ok=False)
            break
        except FileExistsError:
            attempt += 1
    memory_context = memory_backend.retrieve(repository_memory, task, policy=memory_policy) if memory_mode == "repository" else []
    episode_memory: list[MemoryEntry] = []
    result: dict[str, Any] = {
        "task_id": task.id,
        "attempt": attempt,
        "benchmark": task.benchmark,
        "public_issue": task.public_payload(),
        "memory_backend": getattr(memory_backend, "name", "stateportal"),
        "memory_backend_stats": dict(getattr(memory_backend, "last_stats", {})),
        "memory_context": [entry.to_dict() for entry in memory_context],
        "memory_context_by_generation": [],
        "candidates": [],
        "memory_events": [],
        "provider": {"synthesis_attempts": 0, "provider_failures": 0, "fallback_retries": 0},
    }
    try:
        seed_checkpoint, provider_stats = _synthesize_with_retries(
            agent,
            task,
            root,
            task_dir / "synthesis-artifacts",
            provider_retries=provider_retries,
            fallback_agent=fallback_seed_agent,
        )
        result["provider"] = provider_stats
        result["seed_checkpoint"] = seed_checkpoint.to_dict()
        population_now = _fill_population(initial_hypotheses_from_checkpoint(seed_checkpoint), task, population, generation=0)
    except Exception as error:
        incomplete_error = (
            _formal_synthesis_incomplete(error)
            if fail_on_provider_outage
            else None
        )
        if incomplete_error is not None:
            raise incomplete_error from error
        result["synthesis"] = {"status": "failure", "error": str(error)}
        population_now = []

    all_evaluations: list[CandidateEvaluation] = []
    seen: set[str] = set()
    for generation in range(iterations):
        generation_candidates = _unique_hypotheses(population_now, seen)[:population]
        if not generation_candidates:
            break
        generation_evaluations = [
            _evaluate_candidate(
                root,
                task,
                candidate,
                task_dir,
                headless=headless,
                oracle_policy=oracle_policy,
                cascade_config=cascade_config,
                diagnosis_enabled=diagnosis_enabled,
            )
            for candidate in generation_candidates
        ]
        all_evaluations.extend(generation_evaluations)
        result["candidates"].extend(evaluation.to_dict() for evaluation in generation_evaluations)
        if memory_mode in {"episode", "repository"}:
            for evaluation in generation_evaluations:
                if evaluation.hypothesis.used_public_scaffold:
                    continue
                for entry in memory_entries_from_evaluation(task, evaluation, scope=memory_mode, policy=memory_policy):
                    episode_memory.append(entry)
                    result["memory_events"].append(entry.to_dict())
        if generation + 1 >= iterations:
            break
        next_memory_context = _generation_memory_context(memory_mode, task, repository_memory, episode_memory, memory_backend=memory_backend, memory_policy=memory_policy)
        result["memory_context_by_generation"].append(
            {"generation": generation + 1, "entries": [entry.to_dict() for entry in next_memory_context]}
        )
        parents = select_parent_evaluations(generation_evaluations)
        population_now = _next_generation(
            parents,
            task,
            population=population,
            generation=generation + 1,
            memory_context=next_memory_context,
            repair_config=repair_config,
            mutation_policy=mutation_policy,
            model=model,
            fail_on_provider_outage=fail_on_provider_outage,
        )

    final = _select_final_evaluation(all_evaluations)
    if final is not None:
        result["final_evaluation"] = final.to_dict()
        result["final_method_checkpoint"] = final.checkpoint
        result["final_grading"] = _final_grading(root, hidden_oracle, final.checkpoint)
    else:
        result["final_evaluation"] = None
        result["final_method_checkpoint"] = None
        result["final_grading"] = {"strict": {"status": "failure", "reason": "no candidate"}, "scaffold": {"status": "failure", "reason": "no candidate"}}
    result["duration_ms"] = int((time.perf_counter() - task_started) * 1000)
    return result


def _generation_memory_context(
    memory_mode: str,
    task: PublicIssue,
    repository_memory: Sequence[MemoryEntry],
    episode_memory: Sequence[MemoryEntry],
    *,
    memory_backend: MemoryBackend,
    memory_policy: str,
) -> list[MemoryEntry]:
    if memory_mode == "repository":
        return memory_backend.retrieve([*repository_memory, *episode_memory], task, policy=memory_policy)
    if memory_mode == "episode":
        return memory_backend.retrieve(episode_memory, task, policy=memory_policy)
    return []


def _evaluate_candidate(
    root: Path,
    task: PublicIssue,
    hypothesis: CheckpointHypothesis,
    task_dir: Path,
    *,
    headless: bool,
    oracle_policy: OraclePolicyConfig,
    cascade_config: CascadePolicyConfig,
    diagnosis_enabled: bool,
) -> CandidateEvaluation:
    checkpoint = hypothesis.materialize_checkpoint()
    errors = admit_candidate(hypothesis, root)
    if errors:
        diagnosis = ValidationDiagnosis("schema_error", "; ".join(errors), {"admission_errors": errors}, ["schema_repair"])
        return CandidateEvaluation(
            hypothesis=hypothesis,
            admitted=False,
            executed=False,
            diagnosis=diagnosis,
            rank=rank_candidate(hypothesis, diagnosis, None),
            checkpoint=checkpoint,
            runtime_report=None,
            admission_errors=errors,
        )

    spec_path = task_dir / "method-specs" / f"{hypothesis.id}.yaml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(yaml.safe_dump(hypothesis.spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
    try:
        report = run_spec(
            spec_path,
            repo_arg=str(resolve_task_repo(root, task)),
            report_dir=task_dir / "method-runs" / hypothesis.id,
            headless=headless,
            oracle_policy=oracle_policy.policy,
            visual_sample_rate=oracle_policy.visual_sample_rate,
            visual_on_semantic_failure=oracle_policy.visual_on_semantic_failure,
            cascade_config=cascade_config,
        )
        runtime_report = _report_to_dict(report)
        checkpoint["runtime_validation"] = _phase_dict(_get(report, "validation"))
        checkpoint["runtime_visual_validation"] = _phase_dict(_get(report, "visual_oracle_result"))
        diagnosis = diagnose_report(report) if diagnosis_enabled else coarse_diagnose_report(report)
        rank = rank_candidate(hypothesis, diagnosis, report)
    except Exception as error:
        runtime_report = _runtime_exception_report(error)
        checkpoint["runtime_validation"] = runtime_report["validation"]
        checkpoint["runtime_visual_validation"] = runtime_report["visual_oracle_result"]
        diagnosis = diagnose_report(runtime_report) if diagnosis_enabled else coarse_diagnose_report(runtime_report)
        rank = rank_candidate(hypothesis, diagnosis, runtime_report)
    return CandidateEvaluation(
        hypothesis=hypothesis,
        admitted=True,
        executed=True,
        diagnosis=diagnosis,
        rank=rank,
        checkpoint=checkpoint,
        runtime_report=runtime_report,
    )


def _synthesize_with_retries(
    agent: Any,
    task: PublicIssue,
    root: Path,
    artifact_dir: Path,
    *,
    provider_retries: int,
    fallback_agent: Any | None = None,
) -> tuple[MethodCheckpoint, dict[str, Any]]:
    attempts = max(1, provider_retries + 1)
    stats = {"synthesis_attempts": 0, "provider_failures": 0, "fallback_retries": 0, "fallback_seed_agent_used": False}
    last_checkpoint: MethodCheckpoint | None = None
    last_error: Exception | None = None
    for attempt in range(attempts):
        stats["synthesis_attempts"] += 1
        attempt_dir = artifact_dir / f"attempt-{attempt + 1}"
        try:
            checkpoint = agent.synthesize(task, root, artifact_dir=attempt_dir)
            last_checkpoint = checkpoint
            if _provider_retryable_checkpoint(checkpoint) and attempt + 1 < attempts:
                stats["provider_failures"] += 1
                stats["fallback_retries"] += 1
                continue
            if _provider_retryable_checkpoint(checkpoint) and fallback_agent is not None:
                stats["provider_failures"] += 1
                stats["fallback_retries"] += 1
                stats["fallback_seed_agent_used"] = True
                stats["synthesis_attempts"] += 1
                fallback_checkpoint = fallback_agent.synthesize(task, root, artifact_dir=artifact_dir / "fallback-no-explore")
                return fallback_checkpoint, stats
            return checkpoint, stats
        except Exception as error:
            last_error = error
            if _provider_retryable_error(error) and attempt + 1 < attempts:
                stats["provider_failures"] += 1
                stats["fallback_retries"] += 1
                continue
            if _provider_retryable_error(error) and fallback_agent is not None:
                stats["provider_failures"] += 1
                stats["fallback_retries"] += 1
                stats["fallback_seed_agent_used"] = True
                stats["synthesis_attempts"] += 1
                fallback_checkpoint = fallback_agent.synthesize(task, root, artifact_dir=artifact_dir / "fallback-no-explore")
                return fallback_checkpoint, stats
            raise
    if last_checkpoint is not None:
        return last_checkpoint, stats
    assert last_error is not None
    raise last_error


def _provider_retryable_checkpoint(checkpoint: MethodCheckpoint) -> bool:
    data = checkpoint.to_dict()
    if not data.get("used_public_scaffold") or isinstance(data.get("model_generated_spec"), dict):
        return False
    runtime_validation = data.get("runtime_validation")
    text = str(runtime_validation).lower()
    retry_markers = ["empty", "<no value>", "not parse", "not a mapping", "none", "null", "did not produce", "final checkpoint", "max_steps", "no final"]
    return any(marker in text for marker in retry_markers)


def _provider_retryable_error(error: Exception) -> bool:
    text = str(error).lower()
    retry_markers = ["empty", "<no value>", "none", "null", "timeout", "temporar", "gateway", "did not produce", "final checkpoint", "max_steps", "no final"]
    return any(marker in text for marker in retry_markers)


def _final_grading(root: Path, hidden_oracle: Any, checkpoint: dict[str, Any]) -> dict[str, Any]:
    if hidden_oracle is None:
        return {"strict": {"status": "failure", "reason": "missing hidden oracle"}, "scaffold": {"status": "failure", "reason": "missing hidden oracle"}}
    hidden_spec = load_spec(resolve_hidden_spec(root, hidden_oracle))
    return {
        "strict": grade_method_checkpoint_strict(checkpoint, hidden_spec, root),
        "scaffold": grade_method_checkpoint(checkpoint, hidden_spec, root),
        "hidden_oracle_used_for_final_grading_only": True,
    }


def _select_final_evaluation(evaluations: Sequence[CandidateEvaluation]) -> CandidateEvaluation | None:
    eligible = [evaluation for evaluation in evaluations if evaluation.admitted]
    if not eligible:
        return None
    return sorted(eligible, key=lambda item: item.rank, reverse=True)[0]


def _fill_population(hypotheses: Sequence[CheckpointHypothesis], task: PublicIssue, target_size: int, *, generation: int) -> list[CheckpointHypothesis]:
    population = list(hypotheses)
    parents = [hypothesis for hypothesis in hypotheses if not hypothesis.used_public_scaffold]
    index = 0
    while len(population) < target_size and parents:
        parent = parents[index % len(parents)]
        diagnosis = ValidationDiagnosis("schema_error", "Initial population diversity repair.")
        population.extend(_operator_mutations(parent, diagnosis, task, generation=generation, max_new=1))
        index += 1
        if index > target_size * 2:
            break
    return population[:target_size]


def _next_generation(
    parents: Sequence[CandidateEvaluation],
    task: PublicIssue,
    *,
    population: int,
    generation: int,
    memory_context: Sequence[MemoryEntry],
    repair_config: RepairConfig,
    mutation_policy: str,
    model: str,
    fail_on_provider_outage: bool = False,
) -> list[CheckpointHypothesis]:
    children: list[CheckpointHypothesis] = []
    for parent in parents:
        if parent.hypothesis.used_public_scaffold:
            continue
        children.extend(_llm_repair_mutations(
            parent,
            task,
            memory_context,
            repair_config,
            generation=generation,
            max_new=population - len(children),
            fail_on_provider_outage=fail_on_provider_outage,
        ))
        if len(children) >= population:
            break
    if mutation_policy != "none":
        for parent in parents:
            if len(children) >= population:
                break
            if parent.hypothesis.used_public_scaffold:
                continue
            if mutation_policy == "random":
                children.extend(_random_mutations(parent.hypothesis, parent.diagnosis, task, generation=generation, max_new=population))
            else:
                children.extend(_operator_mutations(parent.hypothesis, parent.diagnosis, task, generation=generation, max_new=population))
    if mutation_policy == "source-constrained" and len(parents) >= 2 and len(children) < population:
        crossover = _crossover_mutation(parents[0].hypothesis, parents[1].hypothesis, task, generation=generation)
        if crossover is not None:
            children.append(crossover)
    for entry in memory_context:
        if len(children) >= population:
            break
        child = _hypothesis_from_memory(task, entry, generation=generation)
        if child is not None:
            children.append(child)
    return _unique_hypotheses(children, set())[:population]


def build_repair_prompt(task: PublicIssue, evaluation: CandidateEvaluation, memory_context: Sequence[MemoryEntry]) -> str:
    payload = {
        "task": task.public_payload(),
        "candidate": {
            "id": evaluation.hypothesis.id,
            "generated_spec": _scrub_private(evaluation.hypothesis.spec),
            "source_evidence": _scrub_private(evaluation.hypothesis.source_evidence),
            "payload": _scrub_private(evaluation.hypothesis.payload),
            "mutation": evaluation.hypothesis.mutation,
        },
        "public_diagnosis": _scrub_private(evaluation.diagnosis.to_dict()),
        "public_runtime_report_summary": _public_report_summary(evaluation.runtime_report),
        "memory_snippets": [_scrub_private(entry.to_dict()) for entry in memory_context[:5]],
        "benchmark_contract": _scrub_private(BENCHMARK_CONTRACTS.get(task.benchmark, {})),
        "repair_instructions": _repair_instruction(evaluation.diagnosis.label),
        "output_contract": {
            "type": "one YAML mapping",
            "required": ["generated_spec", "source_evidence", "payload", "runtime_validation", "validity_class"],
            "generated_spec": "WarpTest executable spec mapping with name, benchmark, target, checkpoint, actions, assertions.",
            "runtime_validation": {"status": "pending", "detail": "repair candidate not executed yet"},
        },
    }
    prompt = "\n".join(
        [
            "Repair this EvoPortal checkpoint candidate using only public issue text, public source evidence, public runtime feedback, and public memory.",
            "Do not mention or infer evaluator-only targets, evaluator-only oracles, private grader files, strict grading results, or scaffold answers.",
            "Return YAML only. Do not wrap in Markdown.",
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=False),
        ]
    )
    leaks = detect_private_grader_references(prompt)
    if leaks:
        raise SynthesisError("EvoPortal repair prompt references private grader data.", artifacts={"prompt_private_references": leaks})
    return prompt


def _llm_repair_mutations(
    parent: CandidateEvaluation,
    task: PublicIssue,
    memory_context: Sequence[MemoryEntry],
    repair_config: RepairConfig,
    *,
    generation: int,
    max_new: int,
    fail_on_provider_outage: bool = False,
) -> list[CheckpointHypothesis]:
    if max_new <= 0 or not repair_config.enabled:
        return []
    if parent.diagnosis.label == "success":
        return []
    try:
        prompt = build_repair_prompt(task, parent, memory_context)
        client = repair_config.client or OpenAICompatibleChatClient(
            api_key=os.environ.get(repair_config.api_key_env, ""),
            base_url=repair_config.base_url,
            temperature=repair_config.temperature,
            max_tokens=repair_config.max_tokens,
            extra_headers=repair_config.extra_headers,
        )
        call = client.generate(repair_config.model, prompt, max_attempts=repair_config.max_attempts)
        parsed = parse_llm_checkpoint_output(call.content)
        if detect_private_grader_references(parsed):
            return []
        spec = _repair_spec_from_parsed(parsed, task)
        source_evidence = parsed.get("source_evidence") if isinstance(parsed.get("source_evidence"), list) else parent.hypothesis.source_evidence
        return [
            _make_hypothesis(
                task_id=task.id,
                benchmark=task.benchmark,
                spec=spec,
                source_evidence=[item for item in source_evidence if isinstance(item, dict)],
                inputs_read=parent.hypothesis.inputs_read,
                generation=generation,
                generated_by=repair_config.model,
                parent_ids=[parent.hypothesis.id],
                mutation=f"llm_repair:{parent.diagnosis.label}",
                used_public_scaffold=False,
            )
        ]
    except Exception as error:
        incomplete_error = (
            _formal_synthesis_incomplete(error)
            if fail_on_provider_outage
            else None
        )
        if incomplete_error is not None:
            raise incomplete_error from error
        return []


def _repair_spec_from_parsed(parsed: dict[str, Any], task: PublicIssue) -> dict[str, Any]:
    if isinstance(parsed.get("generated_spec"), dict):
        try:
            normalized, _ = normalize_llm_checkpoint_mapping(parsed, task=task)
            spec = normalized.get("generated_spec")
            if isinstance(spec, dict):
                return spec
        except Exception:
            return copy.deepcopy(parsed["generated_spec"])
    if all(key in parsed for key in ("target", "checkpoint", "actions", "assertions")):
        spec = copy.deepcopy(parsed)
        spec.setdefault("benchmark", task.benchmark)
        spec.setdefault("name", f"EvoPortal repaired checkpoint for {task.id}")
        return spec
    return {"name": f"EvoPortal invalid repair for {task.id}", "benchmark": task.benchmark, "target": {}, "checkpoint": {}, "actions": [], "assertions": []}


def evolution_operator_registry() -> list[EvolutionOperator]:
    return [
        EvolutionOperator("target-field:ensure-kind", "target-field", _op_target_field),
        EvolutionOperator("value:normalize-scalars", "value", _op_normalize_values),
        EvolutionOperator("strategy:allowed-contract", "strategy", _op_allowed_strategy),
        EvolutionOperator("payload:benchmark-setup", "payload", _op_benchmark_setup),
        EvolutionOperator("action:readiness-wait", "action", _op_readiness_wait),
        EvolutionOperator("assertion:public-health", "assertion", _op_public_health_assertion),
        EvolutionOperator("evidence:benchmark-contract", "evidence", _op_evidence_contract),
    ]


def _operator_mutations(
    parent: CheckpointHypothesis,
    diagnosis: ValidationDiagnosis,
    task: PublicIssue,
    *,
    generation: int,
    max_new: int,
) -> list[CheckpointHypothesis]:
    children: list[CheckpointHypothesis] = []
    seen: set[str] = set()
    for operator in evolution_operator_registry():
        for mutation in operator.apply(parent, diagnosis, task):
            fp = _fingerprint({"spec": mutation.spec, "evidence": mutation.source_evidence or parent.source_evidence})
            if fp in seen or fp == _fingerprint({"spec": parent.spec, "evidence": parent.source_evidence}):
                continue
            seen.add(fp)
            children.append(
                _make_hypothesis(
                    task_id=parent.task_id,
                    benchmark=parent.benchmark,
                    spec=mutation.spec,
                    source_evidence=mutation.source_evidence or parent.source_evidence,
                    inputs_read=parent.inputs_read,
                    generation=generation,
                    generated_by=parent.generated_by,
                    parent_ids=[parent.id],
                    mutation=mutation.operator_id,
                    operator_id=mutation.operator_id,
                    used_public_scaffold=False,
                )
            )
            if len(children) >= max_new:
                return children
    return children


def _random_mutations(
    parent: CheckpointHypothesis,
    diagnosis: ValidationDiagnosis,
    task: PublicIssue,
    *,
    generation: int,
    max_new: int,
) -> list[CheckpointHypothesis]:
    operators = sorted(evolution_operator_registry(), key=lambda item: hashlib.sha1(f"{parent.id}:{item.operator_id}".encode("utf-8")).hexdigest())
    children: list[CheckpointHypothesis] = []
    for operator in operators:
        for mutation in operator.apply(parent, diagnosis, task):
            children.append(
                _make_hypothesis(
                    task_id=parent.task_id,
                    benchmark=parent.benchmark,
                    spec=mutation.spec,
                    source_evidence=mutation.source_evidence or parent.source_evidence,
                    inputs_read=parent.inputs_read,
                    generation=generation,
                    generated_by=parent.generated_by,
                    parent_ids=[parent.id],
                    mutation=f"random:{mutation.operator_id}",
                    operator_id=f"random:{mutation.operator_id}",
                    used_public_scaffold=False,
                )
            )
            if len(children) >= max_new:
                return children
    return children


def _op_target_field(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    spec = copy.deepcopy(parent.spec)
    target = spec.setdefault("target", {})
    if not isinstance(target, dict):
        spec["target"] = {"kind": _default_target_kind(task)}
    elif not target.get("kind"):
        target["kind"] = _default_target_kind(task)
    else:
        return []
    return [OperatorMutation("target-field:ensure-kind", "target-field", spec)]


def _op_normalize_values(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    spec = copy.deepcopy(parent.spec)
    changed = _normalize_numeric_strings(spec)
    return [OperatorMutation("value:normalize-scalars", "value", spec)] if changed else []


def _op_allowed_strategy(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    allowed = sorted(_allowed_strategies_for_benchmark(task.benchmark))
    if not allowed:
        return []
    spec = copy.deepcopy(parent.spec)
    checkpoint = spec.setdefault("checkpoint", {})
    if not isinstance(checkpoint, dict):
        spec["checkpoint"] = {"allowed_strategies": [allowed[0]], "expected_validity": "unknown"}
    elif _candidate_strategy(spec) not in set(allowed):
        checkpoint["allowed_strategies"] = [allowed[0]]
    else:
        return []
    return [OperatorMutation("strategy:allowed-contract", "strategy", spec)]


def _op_benchmark_setup(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    base = copy.deepcopy(parent.spec)
    if task.benchmark == "SpaceRPG":
        return [OperatorMutation("payload:benchmark-setup", "payload", _merge_setup(base, {"disable_economy_backend": True}))]
    if task.benchmark == "DatingSim":
        return [OperatorMutation("payload:benchmark-setup", "payload", _merge_setup(base, {"ai_mode": "mocked"}))]
    if task.benchmark == "OpenRA" and _candidate_strategy(base) == "openra_gameplay_production_validate":
        return [OperatorMutation("payload:benchmark-setup", "payload", _merge_setup(base, {"openra_gameplay_probe": {"cash": 50000, "fast_build": True, "build_anywhere": False}}))]
    if task.benchmark == "2048":
        return [OperatorMutation("payload:benchmark-setup", "payload", _ensure_2048_score_fields(base))]
    return []


def _op_readiness_wait(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    if diagnosis.label not in {"render_not_ready", "smoke_action_failed", "soft_locked", "schema_error", "runtime_error"}:
        return []
    return [OperatorMutation("action:readiness-wait", "action", _add_wait_action(parent.spec))]


def _op_public_health_assertion(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    spec = _add_no_severe_console(parent.spec)
    if task.benchmark == "SpaceRPG" and diagnosis.label == "persistence_mismatch":
        spec = _add_spacerpg_save_key_assertion(spec)
    marker = _benchmark_health_assertion(task.benchmark)
    if marker:
        assertions = spec.setdefault("assertions", [])
        if isinstance(assertions, list) and not any(isinstance(item, dict) and item.get("type") == marker for item in assertions):
            assertions.append({"type": marker})
    return [OperatorMutation("assertion:public-health", "assertion", spec)]


def _op_evidence_contract(parent: CheckpointHypothesis, diagnosis: ValidationDiagnosis, task: PublicIssue) -> list[OperatorMutation]:
    evidence = list(parent.source_evidence)
    marker = f"{task.benchmark}:public-contract"
    if any(item.get("matched_symbol") == marker for item in evidence if isinstance(item, dict)):
        return []
    evidence.append({"source_path": "warptest/synthesis.py", "matched_symbol": marker, "rationale": "EvoPortal Phase3 public benchmark contract."})
    return [OperatorMutation("evidence:benchmark-contract", "evidence", copy.deepcopy(parent.spec), evidence)]


def _crossover_mutation(parent_a: CheckpointHypothesis, parent_b: CheckpointHypothesis, task: PublicIssue, *, generation: int) -> CheckpointHypothesis | None:
    if parent_a.used_public_scaffold or parent_b.used_public_scaffold:
        return None
    spec = copy.deepcopy(parent_a.spec)
    spec["actions"] = _merge_sequence(parent_a.spec.get("actions"), parent_b.spec.get("actions"))
    spec["assertions"] = _merge_sequence(parent_a.spec.get("assertions"), parent_b.spec.get("assertions"))
    if not isinstance(spec.get("checkpoint"), dict) and isinstance(parent_b.spec.get("checkpoint"), dict):
        spec["checkpoint"] = copy.deepcopy(parent_b.spec["checkpoint"])
    evidence = [*parent_a.source_evidence, *parent_b.source_evidence]
    return _make_hypothesis(
        task_id=task.id,
        benchmark=task.benchmark,
        spec=spec,
        source_evidence=[item for index, item in enumerate(evidence) if isinstance(item, dict) and item not in evidence[:index]],
        inputs_read=_dedupe([*parent_a.inputs_read, *parent_b.inputs_read]),
        generation=generation,
        generated_by=parent_a.generated_by,
        parent_ids=[parent_a.id, parent_b.id],
        mutation="crossover:two-parent",
        operator_id="crossover:two-parent",
        used_public_scaffold=False,
    )


def _hypothesis_from_memory(task: PublicIssue, entry: MemoryEntry, *, generation: int) -> CheckpointHypothesis | None:
    recipe = entry.checkpoint_recipe
    if not isinstance(recipe, dict) or not isinstance(recipe.get("target"), dict):
        return None
    spec = {
        "name": f"EvoPortal memory candidate for {task.id}",
        "benchmark": task.benchmark,
        "target": copy.deepcopy(recipe.get("target", {})),
        "checkpoint": copy.deepcopy(recipe.get("checkpoint", {"allowed_strategies": [entry.strategy], "expected_validity": "unknown"})),
        "actions": copy.deepcopy(recipe.get("actions", [])),
        "assertions": copy.deepcopy(recipe.get("assertions", [])),
    }
    return _make_hypothesis(
        task_id=task.id,
        benchmark=task.benchmark,
        spec=spec,
        source_evidence=[{"source_path": "", "matched_symbol": symbol, "rationale": "Retrieved EvoPortal public memory."} for symbol in entry.source_symbols[:3]],
        inputs_read=[],
        generation=generation,
        generated_by="evo-memory-replay",
        parent_ids=[entry.id],
        mutation="memory_replay",
        used_public_scaffold=False,
    )


def _make_hypothesis(
    *,
    task_id: str,
    benchmark: str,
    spec: dict[str, Any],
    source_evidence: list[dict[str, Any]],
    inputs_read: list[str],
    generation: int,
    generated_by: str,
    parent_ids: list[str],
    mutation: str,
    used_public_scaffold: bool,
    operator_id: str | None = None,
) -> CheckpointHypothesis:
    spec = _normalize_candidate_spec(copy.deepcopy(spec))
    spec.setdefault("benchmark", benchmark)
    spec.setdefault("name", f"EvoPortal candidate for {task_id}")
    checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
    validity = checkpoint.get("expected_validity") if checkpoint.get("expected_validity") in {"replay-reachable", "save-restorable", "injection-only", "unknown"} else "unknown"
    payload = _payload_for_candidate(benchmark, spec)
    fingerprint = _fingerprint({"task_id": task_id, "generation": generation, "mutation": mutation, "spec": spec, "parents": parent_ids})
    return CheckpointHypothesis(
        id=f"{_slug(task_id)}-g{generation}-{fingerprint[:10]}",
        task_id=task_id,
        benchmark=benchmark,
        generation=generation,
        spec=spec,
        source_evidence=source_evidence or [{"source_path": "", "matched_symbol": benchmark, "rationale": "EvoPortal generated from public issue."}],
        inputs_read=inputs_read,
        payload=payload,
        validity_class=validity,
        generated_by=generated_by,
        parent_ids=parent_ids,
        mutation=mutation,
        operator_id=operator_id or mutation,
        used_public_scaffold=used_public_scaffold,
        cost={"executions": 1},
    )


def _normalize_candidate_spec(spec: dict[str, Any]) -> dict[str, Any]:
    assertions = spec.get("assertions")
    if isinstance(assertions, list):
        for assertion in assertions:
            if (
                isinstance(assertion, dict)
                and assertion.get("type") == "score_increased_by_at_least"
                and "amount" not in assertion
                and "min_increase" in assertion
            ):
                assertion["amount"] = assertion["min_increase"]
    return spec


def _payload_for_candidate(benchmark: str, spec: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(spec)
    candidate.setdefault("benchmark", benchmark)
    return copy.deepcopy(synthesis_payload_for_spec(candidate))


def _candidate_strategy(spec: dict[str, Any]) -> str:
    checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
    strategies = checkpoint.get("allowed_strategies")
    if isinstance(strategies, list) and strategies:
        return str(strategies[0])
    return "unknown"


def _strategy_allowed(benchmark: str, spec: dict[str, Any]) -> bool:
    allowed = _allowed_strategies_for_benchmark(benchmark)
    return bool(allowed) and _candidate_strategy(spec) in allowed


def _allowed_strategies_for_benchmark(benchmark: str) -> set[str]:
    contract = BENCHMARK_CONTRACTS.get(benchmark)
    if not isinstance(contract, dict):
        return set()
    generated = contract.get("generated_spec")
    if isinstance(generated, dict):
        checkpoint = generated.get("checkpoint")
        strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else None
        if isinstance(strategies, list):
            return {str(strategy) for strategy in strategies}
    variants = contract.get("schema_variants")
    allowed: set[str] = set()
    if isinstance(variants, dict):
        for variant in variants.values():
            if not isinstance(variant, dict):
                continue
            generated = variant.get("generated_spec")
            checkpoint = generated.get("checkpoint") if isinstance(generated, dict) else None
            strategies = checkpoint.get("allowed_strategies") if isinstance(checkpoint, dict) else None
            if isinstance(strategies, list):
                allowed.update(str(strategy) for strategy in strategies)
    return allowed


def _unique_hypotheses(hypotheses: Sequence[CheckpointHypothesis], seen: set[str]) -> list[CheckpointHypothesis]:
    result: list[CheckpointHypothesis] = []
    for hypothesis in hypotheses:
        fp = _fingerprint(hypothesis.spec)
        if fp in seen:
            continue
        seen.add(fp)
        result.append(hypothesis)
    return result


def _add_no_severe_console(spec: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(spec)
    assertions = mutated.setdefault("assertions", [])
    if isinstance(assertions, list) and not any(isinstance(item, dict) and item.get("type") == "no_severe_console" for item in assertions):
        assertions.append({"type": "no_severe_console"})
    return mutated


def _default_target_kind(task: PublicIssue) -> str:
    return {
        "2048": "board",
        "SpaceRPG": "state_patch",
        "DatingSim": "deterministic_state",
        "OpenRA": "openra_map",
        "JupyterLab": "text_file",
        "LibreOffice": "writer_document",
        "Blender": "scene",
        "jynew": "synthesized_state",
        "pal3": "synthesized_state",
        "luanti": "synthesized_world",
    }.get(task.benchmark, "state")


def _normalize_numeric_strings(value: Any) -> bool:
    changed = False
    if isinstance(value, dict):
        for key, item in list(value.items()):
            if isinstance(item, str) and re.fullmatch(r"-?\d+", item):
                value[key] = int(item)
                changed = True
            else:
                changed = _normalize_numeric_strings(item) or changed
    elif isinstance(value, list):
        for item in value:
            changed = _normalize_numeric_strings(item) or changed
    return changed


def _benchmark_health_assertion(benchmark: str) -> str | None:
    return {
        "jynew": "no_jynew_utility_errors",
        "pal3": "no_pal3_utility_errors",
        "luanti": "no_luanti_mod_errors",
        "JupyterLab": "no_jupyterlab_server_errors",
        "LibreOffice": "no_libreoffice_errors",
        "Blender": "no_blender_python_errors",
    }.get(benchmark)


def _merge_sequence(left: Any, right: Any) -> list[Any]:
    merged: list[Any] = []
    for value in [left, right]:
        if not isinstance(value, list):
            continue
        for item in value:
            if item not in merged:
                merged.append(copy.deepcopy(item))
    return merged


def _add_wait_action(spec: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(spec)
    actions = mutated.setdefault("actions", [])
    if isinstance(actions, list):
        actions.append({"type": "wait", "ms": 300})
    return mutated


def _merge_setup(spec: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(spec)
    setup = mutated.setdefault("setup", {})
    if isinstance(setup, dict):
        setup.update(values)
    else:
        mutated["setup"] = copy.deepcopy(values)
    return mutated


def _ensure_2048_score_fields(spec: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(spec)
    target = mutated.setdefault("target", {})
    if isinstance(target, dict):
        target.setdefault("score", 0)
        target.setdefault("bestScore", target.get("score", 0))
    return mutated


def _add_spacerpg_save_key_assertion(spec: dict[str, Any]) -> dict[str, Any]:
    mutated = copy.deepcopy(spec)
    actions = mutated.get("actions", [])
    slot = None
    if isinstance(actions, list):
        for action in actions:
            if isinstance(action, dict) and action.get("type") == "hook_call" and action.get("method") == "saveGame":
                args = action.get("args")
                if isinstance(args, list) and args:
                    slot = str(args[0])
                    break
    if not slot:
        slot = "auto"
    assertions = mutated.setdefault("assertions", [])
    key = f"spacerpg-save-slot:{slot}"
    if isinstance(assertions, list) and not any(isinstance(item, dict) and item.get("type") == "localStorage_key_exists" and item.get("key") == key for item in assertions):
        assertions.append({"type": "localStorage_key_exists", "key": key})
    return mutated


def _memory_entry_from_dict(data: dict[str, Any]) -> MemoryEntry:
    return MemoryEntry(
        id=str(data.get("id", "")),
        scope=str(data.get("scope", "repository")),
        benchmark=str(data.get("benchmark", "")),
        semantic_surface=str(data.get("semantic_surface", "")),
        issue_keywords=[str(item) for item in data.get("issue_keywords", [])],
        strategy=str(data.get("strategy", "")),
        failure_signature=str(data.get("failure_signature", "")),
        checkpoint_recipe=data.get("checkpoint_recipe", {}) if isinstance(data.get("checkpoint_recipe"), dict) else {},
        public_validation_summary=data.get("public_validation_summary", {}) if isinstance(data.get("public_validation_summary"), dict) else {},
        source_symbols=[str(item) for item in data.get("source_symbols", [])],
        repair_history=[item for item in data.get("repair_history", []) if isinstance(item, dict)],
        confidence=float(data.get("confidence", 0.5) or 0.5),
        support_count=int(data.get("support_count", 1) or 1),
        entry_type=str(data.get("entry_type", "validated_recipe")),
        score=float(data.get("score", 0.0) or 0.0),
        text_summary=str(data.get("text_summary", "")),
    )


def _report_to_dict(report: RunReport | dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(report, dict):
        return copy.deepcopy(report)
    to_dict = getattr(report, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    data: dict[str, Any] = {}
    for key in [
        "target_validated_before_smoke",
        "checkpoint_synthesis",
        "checkpoint_restoration",
        "validation",
        "smoke_execution",
        "oracle_result",
        "visual_oracle_result",
        "overall_status",
        "memory_persistence_diff",
        "soft_lock_signal",
        "render_readiness_signal",
        "post_checkpoint_interaction",
        "console_errors",
        "network_api_calls",
        "hook_availability",
        "screenshots",
        "metrics",
    ]:
        value = _get(report, key)
        if value is not None:
            data[key] = _phase_dict(value) if isinstance(value, PhaseResult) else copy.deepcopy(value)
    for key in ["checkpoint_synthesis", "checkpoint_restoration", "validation", "smoke_execution", "oracle_result", "visual_oracle_result"]:
        if key in data:
            data[key] = _phase_dict(data[key])
    return data


def _runtime_exception_report(error: Exception) -> dict[str, Any]:
    detail = str(error)
    failure = {"status": "failure", "detail": detail, "data": {}, "duration_ms": None}
    skipped = {"status": "skipped", "detail": "run_spec failed before this phase.", "data": {}, "duration_ms": None}
    return {
        "target_validated_before_smoke": False,
        "checkpoint_restoration": failure,
        "validation": failure,
        "smoke_execution": skipped,
        "oracle_result": skipped,
        "visual_oracle_result": skipped,
        "overall_status": "failure",
        "memory_persistence_diff": {},
        "soft_lock_signal": {},
        "render_readiness_signal": {},
        "post_checkpoint_interaction": {},
        "console_errors": [detail],
        "network_api_calls": {},
        "hook_availability": {},
        "screenshots": [],
        "metrics": {"run_spec_exception": type(error).__name__},
    }


def _phase_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, PhaseResult):
        return asdict(value)
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if value is None:
        return {"status": "unknown"}
    status = getattr(value, "status", None)
    detail = getattr(value, "detail", "")
    data = getattr(value, "data", {})
    duration_ms = getattr(value, "duration_ms", None)
    return {"status": status or "unknown", "detail": detail or "", "data": data if isinstance(data, dict) else {}, "duration_ms": duration_ms}


def _phase_status(value: Any) -> str:
    if isinstance(value, PhaseResult):
        return value.status
    if isinstance(value, dict):
        return str(value.get("status", "unknown"))
    return str(getattr(value, "status", "unknown"))


def _phase_detail(value: Any) -> str:
    if isinstance(value, PhaseResult):
        return value.detail
    if isinstance(value, dict):
        return str(value.get("detail", ""))
    return str(getattr(value, "detail", ""))


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _meaningful_mapping(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    if value.get("ok") is True or value.get("matches") is True or value.get("equal") is True:
        return False
    if value.get("diff_count") in {0, "0"}:
        return False
    harmless = {"status", "ok", "matches", "equal", "diff_count"}
    if set(value) <= harmless and value.get("diff_count") is None:
        return False
    return any(item not in ({}, [], None, "", False, 0) for item in value.values())


def _render_not_ready(signal: Any) -> bool:
    if not isinstance(signal, dict) or not signal:
        return False
    for key, value in signal.items():
        if key in {"ready", "canvas_ready", "dom_ready", "webgl_ready", "visible"} and value is False:
            return True
        if key.endswith("_ready") and value is False:
            return True
    return str(signal.get("status", "")).lower() in {"failure", "not_ready", "timeout"}


def _soft_locked(signal: Any) -> bool:
    if not isinstance(signal, dict) or not signal:
        return False
    if signal.get("soft_locked") is True or signal.get("blocked") is True:
        return True
    return str(signal.get("status", "")).lower() in {"failure", "soft_locked", "blocked"}


def _post_interaction_failed(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    return str(value.get("status", value.get("result", ""))).lower() in {"failure", "failed", "blocked", "soft_locked"}


def _hook_unavailable(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    if value.get("available") is False:
        return True
    return str(value.get("status", "")).lower() in {"missing", "unavailable", "failure"}


def _mentions(text: str, needle: str) -> bool:
    return needle.lower() in text.lower()


def _keywords(issue: str, tags: Iterable[str]) -> list[str]:
    words = re.findall(r"[a-zA-Z0-9_+-]+", issue.lower())
    words.extend(str(tag).lower() for tag in tags)
    stop = {"the", "and", "that", "with", "from", "into", "can", "verify", "check"}
    result: list[str] = []
    seen: set[str] = set()
    for word in words:
        if len(word) < 3 or word in stop or word in seen:
            continue
        seen.add(word)
        result.append(word)
    return result[:24]


def _source_symbols(evidence: Sequence[dict[str, Any]]) -> list[str]:
    symbols: list[str] = []
    for item in evidence:
        for key in ("matched_symbol", "symbol", "source_path"):
            value = item.get(key)
            if isinstance(value, str) and value and value not in symbols:
                symbols.append(value)
    return symbols[:12]


def _scrub_private(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(keyword in lowered for keyword in PRIVATE_MEMORY_KEYWORDS):
                continue
            result[key] = _scrub_private(item)
        return result
    if isinstance(value, list):
        return [_scrub_private(item) for item in value]
    if isinstance(value, str) and detect_private_grader_references(value):
        return "[redacted-private-reference]"
    return copy.deepcopy(value)


def load_evo_memory(path: str | Path) -> list[MemoryEntry]:
    data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return []
    entries = data.get("entries")
    if not isinstance(entries, list):
        return []
    result: list[MemoryEntry] = []
    for entry in entries:
        if isinstance(entry, dict):
            scrubbed = _scrub_private(entry)
            if isinstance(scrubbed, dict):
                result.append(_memory_entry_from_dict(scrubbed))
    return result


def _public_report_summary(report: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(report, dict):
        return {}
    keys = [
        "target_validated_before_smoke",
        "checkpoint_restoration",
        "validation",
        "smoke_execution",
        "oracle_result",
        "memory_persistence_diff",
        "soft_lock_signal",
        "render_readiness_signal",
        "post_checkpoint_interaction",
        "console_errors",
        "network_api_calls",
        "hook_availability",
        "metrics",
    ]
    summary = {key: _scrub_private(report.get(key)) for key in keys if key in report}
    if isinstance(summary.get("console_errors"), list):
        summary["console_errors"] = summary["console_errors"][:5]
    return summary


def _repair_instruction(label: str) -> dict[str, Any]:
    instructions = {
        "schema_error": "Return a valid method checkpoint mapping and executable generated_spec; do not use public scaffold fallback.",
        "target_state_mismatch": "Repair target fields using source vocabulary and public validation evidence.",
        "hook_unavailable": "Use only the benchmark contract's public allowed strategy and public hook names.",
        "persistence_mismatch": "Add or align public save/localStorage/persistence fields and roundtrip assertions.",
        "render_not_ready": "Add readiness waits or public readiness preconditions without changing hidden targets.",
        "soft_locked": "Add publicly observable preconditions so the smoke action can still run.",
        "smoke_action_failed": "Repair or replace the smoke action within the public action contract.",
        "public_invariant_failed": "Repair public assertions/actions so the public invariant can pass.",
        "runtime_error": "Repair payload/setup/action fields that caused public runtime failure.",
    }
    return {"diagnosis": label, "instruction": instructions.get(label, "Repair the candidate using public feedback only.")}


def _memory_query_terms(task: PublicIssue) -> list[str]:
    return _dedupe(_keywords(task.issue, task.tags) + [task.benchmark.lower()])


def _memory_document_terms(entry: MemoryEntry) -> list[str]:
    terms = [
        entry.benchmark.lower(),
        entry.semantic_surface.lower(),
        entry.strategy.lower(),
        entry.failure_signature.lower(),
        entry.entry_type.lower(),
    ]
    terms.extend(term.lower() for term in entry.issue_keywords)
    terms.extend(term.lower() for term in entry.source_symbols)
    terms.extend(re.findall(r"[a-zA-Z0-9_+-]+", entry.text_summary.lower()))
    recipe = entry.checkpoint_recipe
    if isinstance(recipe, dict):
        terms.extend(re.findall(r"[a-zA-Z0-9_+-]+", yaml.safe_dump(recipe, sort_keys=True, allow_unicode=False).lower()))
    return [term for term in terms if term]


def _bm25_score(query_terms: Sequence[str], doc_terms: Sequence[str], docs: Sequence[Sequence[str]], avg_len: float) -> float:
    if not query_terms or not doc_terms or not docs:
        return 0.0
    k1 = 1.2
    b = 0.75
    doc_len = max(1, len(doc_terms))
    avg_len = avg_len or doc_len
    score = 0.0
    for term in query_terms:
        tf = sum(1 for item in doc_terms if item == term)
        if tf <= 0:
            continue
        df = sum(1 for doc in docs if term in doc)
        idf = max(0.0, ((len(docs) - df + 0.5) / (df + 0.5)))
        score += idf * ((tf * (k1 + 1)) / (tf + k1 * (1 - b + b * doc_len / avg_len)))
    return score


def _schema_key_bonus(query_terms: Sequence[str], entry: MemoryEntry) -> float:
    query = set(query_terms)
    bonus = 0.0
    for key in [entry.semantic_surface, entry.strategy, entry.failure_signature, entry.entry_type]:
        if key and key.lower() in query:
            bonus += 1.0
    if entry.entry_type == "validated_recipe":
        bonus += 0.25
    return bonus


def _fingerprint(value: Any) -> str:
    payload = yaml.safe_dump(value, sort_keys=True, allow_unicode=False)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def _novelty_key(hypothesis: CheckpointHypothesis) -> int:
    return int(_fingerprint({"target": hypothesis.spec.get("target"), "actions": hypothesis.spec.get("actions")})[:6], 16)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return slug or "candidate"


def _dedupe(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result
