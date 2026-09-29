"""Trusted runtime adapters for C1-Lazarus Phase 0.

The model-facing synthesis call is deliberately narrower than the runtime call:
it receives a freshly reconstructed :class:`PublicIssue` and a clean source-only
tree, while the trusted execution backend alone receives a versioned artifact
and private oracle-derived semantics.  The state arm cache is owned here rather
than by a provider callback so the fixed cell cannot silently issue a second
synthesis request.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Protocol

import yaml

from . import cost
from .c1_openra_runtime import OpenRAGameplaySession
from .c1_provider_runtime import C1_PROVIDER_TIMEOUT_S, provider_timeout_environment
from .c1_sandbox import strict_clone_tree
from .c1_spec_acceptance import accept_c1_generated_spec
from .luanti_runtime import sync_luanti_builtin_assets
from .luanti_state import LuantiWarmSession
from .lazarus_openra_hotkey_probe import (
    OPENRA_HOTKEY_AUDIT_ENV,
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    OPENRA_HOTKEY_STATE_ENV,
    _wait_for_hotkey_audit,
    evaluate_openra_pr21453_hotkey_audit,
    canonicalize_openra_pr21453_checkpoint_semantics,
    openra_pr21453_checkpoint_semantics_sha256,
    openra_pr21453_hotkey_probe,
    run_openra_pr21453_hotkey_cycle_probe,
    run_openra_pr21453_hotkey_probe,
)
from .lazarus_phase0 import LazarusPhase0CellContext
from .lazarus_luanti_start import (
    LUANTI_VECTOR_START,
    LuantiPhase0Start,
    luanti_live_assertions,
    luanti_start_for_task_id,
)
from .lazarus_probes import luanti_pr11919_public_view_assertion
from .lazarus_runtime_support import paid_runtime_support_failures
from .lazarus_trial import LazarusRuntimeObservation
from .lazarus_versions import tree_fingerprint
from .providers import resolve_model
from .synthesis import build_synthesis_agent
from .task_schema import PublicIssue, task_repo_override


_FINAL_VERDICT_FIELDS = frozenset(
    {"start_success", "goal_success", "defect_exposed", "false_alarm"}
)
PHASE0_OBSERVER_MODEL = "claude-sonnet-5@relay"
_LUANTI_VECTOR_PHASE0_TASK_ID = LUANTI_VECTOR_START.task_id
# The pilot's OpenRA wording names the map it launches, so Agent-S is told which
# skirmish it is looking at.  This is a Phase-0 public-text contract, not a
# runtime capability.
_PHASE0_PUBLIC_START_TOKENS = {OPENRA_HOTKEY_PHASE0_TASK_ID: "allies-08a"}
_LUANTI_PUBLIC_START_SETTLE_S = 16.0
_LUANTI_PUBLIC_TRANSITION_SETTLE_S = 2.0
_LUANTI_PUBLIC_START_VIEW_ATTEMPTS = 3


@dataclass(frozen=True)
class LazarusSynthesizedCheckpoint:
    """One actual state-synthesis outcome before coordinator execution."""

    generated_spec: Mapping[str, Any]
    actual_synthesis_calls: int
    treatment_tokens: int | float
    treatment_time_ms: int | float
    trace: Mapping[str, Any]


@dataclass(frozen=True)
class LazarusRuntimeExecution:
    """Raw runtime observation emitted without a final Lazarus verdict."""

    complete: bool
    runtime_restored: bool
    before_semantic_ok: bool | None
    goal_checks_ok: bool | None
    defect_signature_ok: bool | None
    invariants_ok: bool | None
    before_screenshot: Path | None
    after_screenshot: Path | None
    runtime_time_ms: int | float
    trace: Mapping[str, Any]
    p2p_restore_adapter: Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]] | None = None
    # Where this cell's per-step frames and trajectory records were written.
    # Kept outside ``trace`` because the trace feeds the run fingerprint.
    artifact_dir: Path | None = None


class LazarusStateSynthesis(Protocol):
    """Model-facing protocol; no versioned artifact or private task data crosses it."""

    def synthesize(
        self,
        issue: PublicIssue,
        source_root: Path,
        artifact_dir: Path,
        *,
        seed: int,
        model: str,
    ) -> LazarusSynthesizedCheckpoint: ...


class LazarusStateExecution(Protocol):
    """Coordinator runtime protocol, allowed to access its selected artifact."""

    def execute(
        self,
        context: LazarusPhase0CellContext,
        generated_spec: Mapping[str, Any],
        *,
        checkpoint_sha256: str,
    ) -> LazarusRuntimeExecution: ...


class LazarusAgentSExecution(Protocol):
    """From-start GUI runtime protocol with no access to final verdict fields."""

    def execute(
        self, context: LazarusPhase0CellContext
    ) -> LazarusRuntimeExecution: ...


class LazarusLLMStateSynthesis:
    """Production state synthesis with a clean-source task-repository override."""

    def __init__(
        self,
        *,
        workspace: str | Path,
        base_url: str = "https://api-inference.modelscope.cn/v1",
        api_key_env: str = "MODELSCOPE_API_KEY",
        temperature: float = 0.2,
        max_tokens: int = 8192,
        explore: bool = True,
        max_explore_steps: int = 12,
        agent_factory: Any = build_synthesis_agent,
        provider_resolver: Any = resolve_model,
        acceptor: Any | None = None,
        usage_summary: Any | None = None,
        provider_timeout_s: float = C1_PROVIDER_TIMEOUT_S,
    ) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._explore = explore
        self._max_explore_steps = max_explore_steps
        self._agent_factory = agent_factory
        self._provider_resolver = provider_resolver
        self._acceptor = acceptor or _accept_state_generated_spec
        self._usage_summary = usage_summary
        if (
            isinstance(provider_timeout_s, bool)
            or not isinstance(provider_timeout_s, (int, float))
            or not math.isfinite(float(provider_timeout_s))
            or float(provider_timeout_s) != C1_PROVIDER_TIMEOUT_S
        ):
            raise ValueError(
                "Lazarus state synthesis timeout must be the registered 900 seconds"
            )
        self._provider_timeout_s = float(provider_timeout_s)

    def synthesize(
        self,
        issue: PublicIssue,
        source_root: Path,
        artifact_dir: Path,
        *,
        seed: int,
        model: str,
    ) -> LazarusSynthesizedCheckpoint:
        source = Path(source_root).expanduser().resolve()
        if issue.source_path is not None:
            raise ValueError("Lazarus synthesis issue must not expose its catalog path")
        if not source.is_dir() or (source / ".git").exists():
            raise ValueError("Lazarus synthesis requires a clean source-only tree")
        clean_model, provider = self._provider_resolver(
            model,
            default_base_url=self._base_url,
            default_api_key_env=self._api_key_env,
        )
        agent = self._agent_factory(
            agent="llm",
            model=clean_model,
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            explore=self._explore,
            max_steps=self._max_explore_steps,
            extra_headers=dict(provider.extra_headers),
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        public_repo = (self._workspace / issue.repo).resolve()
        started = time.perf_counter()
        with task_repo_override(public_repo, source):
            with provider_timeout_environment(self._provider_timeout_s):
                if self._usage_summary is None:
                    with cost.scoped_meter() as meter:
                        try:
                            checkpoint = agent.synthesize(
                                issue, self._workspace, artifact_dir
                            )
                        except Exception as error:
                            setattr(error, "lazarus_usage", meter.summary())
                            raise
                    usage = meter.summary()
                else:
                    try:
                        checkpoint = agent.synthesize(
                            issue, self._workspace, artifact_dir
                        )
                    except Exception as error:
                        setattr(error, "lazarus_usage", self._usage_summary())
                        raise
                    usage = self._usage_summary()
        try:
            if getattr(checkpoint, "hidden_files_read", None):
                raise ValueError("Lazarus state synthesis accessed hidden files")
            if getattr(checkpoint, "used_public_scaffold", False) is True:
                raise ValueError("Lazarus state synthesis fell back to a public scaffold")
            generated = self._acceptor(
                getattr(checkpoint, "model_generated_spec", None), issue=issue, seed=seed
            )
            if not isinstance(generated, Mapping) or not generated:
                raise ValueError("Lazarus state synthesis produced no accepted checkpoint")
        except Exception as error:
            _attach_lazarus_usage(error, usage)
            raise
        try:
            calls = _usage_count(usage, "llm_calls")
            tokens = _usage_count(usage, "total_tokens")
            trace: dict[str, Any] = {
                "schema": "c1-lazarus-llm-synthesis-v1",
                "provider": str(provider.name),
                "model": str(clean_model),
                "provider_timeout_s": self._provider_timeout_s,
                "usage": {"llm_calls": calls, "total_tokens": tokens},
                # The agent already writes prompt.txt/raw-response.txt/transcript.json
                # here; naming the directory is what lets a reader find them.
                "artifact_dir": str(artifact_dir),
            }
            ledger = _persist_cost_ledger(artifact_dir, usage)
            if ledger is not None:
                trace["cost_ledger"] = ledger
            return LazarusSynthesizedCheckpoint(
                generated_spec=copy.deepcopy(dict(generated)),
                actual_synthesis_calls=calls,
                treatment_tokens=tokens,
                treatment_time_ms=int((time.perf_counter() - started) * 1000),
                trace=trace,
            )
        except Exception as error:
            _attach_lazarus_usage(error, usage)
            raise


@dataclass(frozen=True)
class _CachedCheckpoint:
    generated_spec: dict[str, Any]
    checkpoint_sha256: str


class LazarusStateRuntimeRunner:
    """Run the state arm with exact buggy-to-fixed checkpoint reuse.

    The object is deliberately process-local for Phase 0's serial eight-cell
    pilot.  It rejects fixed-first dispatch rather than regenerating a state and
    invalidating H3's zero-new-synthesis contract.
    """

    def __init__(
        self,
        *,
        synthesis: LazarusStateSynthesis,
        execution: LazarusStateExecution,
        artifact_root: str | Path,
    ) -> None:
        self._synthesis = synthesis
        self._execution = execution
        self._artifact_root = Path(artifact_root).expanduser().resolve()
        self._cache: dict[tuple[str, str, int, str], _CachedCheckpoint] = {}

    def __call__(self, context: LazarusPhase0CellContext) -> LazarusRuntimeObservation:
        key = context.key
        if key.arm != "lazarus_state":
            raise ValueError("LazarusStateRuntimeRunner accepts only lazarus_state cells")
        if context.agent_source.version_identity_hidden is not True:
            raise ValueError("state synthesis source must hide version identity")
        source_root = context.agent_source.root.expanduser().resolve()
        if not source_root.is_dir() or (source_root / ".git").exists():
            raise ValueError("state synthesis source is not an isolated clean tree")
        cache_key = (
            key.task_id,
            key.model,
            key.seed,
            context.agent_source.sanitized_tree_sha256,
        )
        public_issue = _public_issue(context)
        checkpoint, cache_hit, synthesis = self._checkpoint_for(
            context,
            cache_key=cache_key,
            issue=public_issue,
            source_root=source_root,
        )
        try:
            execution = self._execution.execute(
                context,
                copy.deepcopy(checkpoint.generated_spec),
                checkpoint_sha256=checkpoint.checkpoint_sha256,
            )
        except Exception as error:
            _attach_lazarus_usage(error, _synthesis_usage(synthesis, cache_hit=cache_hit))
            raise
        try:
            _validate_execution(execution)
            runtime_trace = {
                "schema": "c1-lazarus-state-runtime-v1",
                "synthesis": {
                    "cache_hit": cache_hit,
                    "actual_synthesis_calls": (
                        0 if cache_hit else synthesis.actual_synthesis_calls
                    ),
                    "trace": dict(synthesis.trace),
                },
                "execution": dict(execution.trace),
            }
            _reject_final_verdict(runtime_trace, "state runtime trace")
            observation = LazarusRuntimeObservation(
                complete=execution.complete,
                runtime_restored=execution.runtime_restored,
                before_semantic_ok=execution.before_semantic_ok,
                goal_checks_ok=execution.goal_checks_ok,
                defect_signature_ok=execution.defect_signature_ok,
                invariants_ok=execution.invariants_ok,
                before_screenshot=execution.before_screenshot,
                after_screenshot=execution.after_screenshot,
                checkpoint_sha256=checkpoint.checkpoint_sha256,
                synthesis_cache_hit=cache_hit,
                actual_synthesis_calls=(
                    0 if cache_hit else synthesis.actual_synthesis_calls
                ),
                checkpoint_reuse_success=True if cache_hit else False,
                treatment_tokens=0 if cache_hit else synthesis.treatment_tokens,
                treatment_time_ms=(
                    execution.runtime_time_ms
                    if cache_hit
                    else float(synthesis.treatment_time_ms)
                    + float(execution.runtime_time_ms)
                ),
                trace=runtime_trace,
                p2p_restore_adapter=execution.p2p_restore_adapter,
            )
        except Exception as error:
            _attach_lazarus_usage(
                error, _synthesis_usage(synthesis, cache_hit=cache_hit)
            )
            raise
        return observation

    def _checkpoint_for(
        self,
        context: LazarusPhase0CellContext,
        *,
        cache_key: tuple[str, str, int, str],
        issue: PublicIssue,
        source_root: Path,
    ) -> tuple[_CachedCheckpoint, bool, LazarusSynthesizedCheckpoint]:
        if context.key.version == "fixed":
            cached = self._cache.get(cache_key)
            if cached is None:
                raise ValueError(
                    "fixed Lazarus state cell requires the registered buggy checkpoint"
                )
            return (
                cached,
                True,
                LazarusSynthesizedCheckpoint(
                    generated_spec=cached.generated_spec,
                    actual_synthesis_calls=0,
                    treatment_tokens=0,
                    treatment_time_ms=0,
                    trace={"source": "checkpoint_cache"},
                ),
            )
        if context.key.version != "buggy":
            raise ValueError("Lazarus state version is invalid")
        if cache_key in self._cache:
            raise ValueError("buggy Lazarus state checkpoint is already registered")
        artifact_dir = self._synthesis_artifact_dir(context)
        synthesis = self._synthesis.synthesize(
            issue,
            source_root,
            artifact_dir,
            seed=context.key.seed,
            model=context.key.model,
        )
        try:
            _validate_synthesis(synthesis)
            generated_spec = copy.deepcopy(dict(synthesis.generated_spec))
            checkpoint = _CachedCheckpoint(
                generated_spec=generated_spec,
                checkpoint_sha256=_checkpoint_sha256(generated_spec),
            )
        except Exception as error:
            _attach_lazarus_usage(
                error, _synthesis_usage(synthesis, cache_hit=False)
            )
            raise
        self._cache[cache_key] = checkpoint
        return checkpoint, False, synthesis

    def _synthesis_artifact_dir(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._artifact_root / context.key.invocation_id() / "synthesis-artifacts"
        ).resolve()
        if self._artifact_root != destination and self._artifact_root not in destination.parents:
            raise ValueError("state synthesis artifact path escapes its root")
        destination.mkdir(parents=True, exist_ok=False)
        return destination


class LazarusAgentSRuntimeRunner:
    """Run one independent Agent-S attempt for each selected software version.

    Unlike :class:`LazarusStateRuntimeRunner`, this runner deliberately owns no
    checkpoint cache.  The buggy and fixed cells each enter their runtime from
    scratch, and the scoped meter records the complete policy/grounder spend of
    that individual attempt.  ``actual_synthesis_calls`` consequently remains
    zero: Agent-S policy calls are treatment cost, not state synthesis.
    """

    def __init__(
        self,
        *,
        execution: LazarusAgentSExecution,
        observer_model: str = PHASE0_OBSERVER_MODEL,
    ) -> None:
        if observer_model != PHASE0_OBSERVER_MODEL:
            raise ValueError("Lazarus Agent-S observer model does not match the manifest")
        self._execution = execution
        self._observer_model = observer_model

    def __call__(self, context: LazarusPhase0CellContext) -> LazarusRuntimeObservation:
        key = context.key
        if key.arm != "lazarus_agent_s":
            raise ValueError("LazarusAgentSRuntimeRunner accepts only lazarus_agent_s cells")
        if context.agent_source.version_identity_hidden is not True:
            raise ValueError("Agent-S source contract must hide version identity")
        source_root = context.agent_source.root.expanduser().resolve()
        if not source_root.is_dir() or (source_root / ".git").exists():
            raise ValueError("Agent-S source contract is not an isolated clean tree")
        with cost.scoped_meter() as meter:
            try:
                execution = self._execution.execute(context)
            except Exception as error:
                _attach_lazarus_usage(error, meter.summary())
                raise
        usage = meter.summary()
        try:
            _validate_execution(execution)
            if usage.get("usage_complete") is not True:
                raise ValueError("Agent-S policy usage accounting is incomplete")
            tokens = _usage_count(usage, "total_tokens")
            runtime_trace = {
                "schema": "c1-lazarus-agent-s-runtime-v1",
                "checkpoint_kind": "not_applicable_from_start",
                "policy_usage": {
                    "llm_calls": _usage_count(usage, "llm_calls"),
                    "total_tokens": tokens,
                    "usage_complete": True,
                },
                "observer": {
                    "model": self._observer_model,
                    "usage": "registered_not_consumed_native_screenshot",
                    "llm_calls": 0,
                },
                "execution": dict(execution.trace),
                "trajectory": _record_agent_s_trajectory(execution.artifact_dir, usage),
            }
            _reject_final_verdict(runtime_trace, "Agent-S runtime trace")
            run_sha256 = _checkpoint_sha256(
                {
                    "schema": "c1-lazarus-agent-s-run-v1",
                    "identity": key.identity(),
                    "execution": dict(execution.trace),
                }
            )
            observation = LazarusRuntimeObservation(
                complete=execution.complete,
                runtime_restored=execution.runtime_restored,
                before_semantic_ok=execution.before_semantic_ok,
                goal_checks_ok=execution.goal_checks_ok,
                defect_signature_ok=execution.defect_signature_ok,
                invariants_ok=execution.invariants_ok,
                before_screenshot=execution.before_screenshot,
                after_screenshot=execution.after_screenshot,
                checkpoint_sha256=run_sha256,
                synthesis_cache_hit=None,
                actual_synthesis_calls=0,
                checkpoint_reuse_success=None,
                treatment_tokens=tokens,
                treatment_time_ms=execution.runtime_time_ms,
                trace=runtime_trace,
                p2p_restore_adapter=execution.p2p_restore_adapter,
            )
        except Exception as error:
            _attach_lazarus_usage(error, usage)
            raise
        return observation


class LazarusPhase0RuntimeUnsupportedError(RuntimeError):
    """Raised before dispatch when a registered pilot lacks a valid runtime arm."""


def validate_lazarus_phase0_runtime_support(
    cells: Sequence[LazarusPhase0CellContext | Mapping[str, Any]],
) -> None:
    """Reject unsupported Phase-0 cells before any provider-backed dispatch.

    OpenRA #21453 is eligible only through its registered adapter: Agent-S is
    started from the raw ready-building condition without the state environment,
    while the state arm receives the private restore request.  Keeping this
    check outside the cell loop prevents any provider call from being spent if a
    future pilot task lacks that same arm-specific boundary.

    The admitted population lives in :mod:`warptest.lazarus_runtime_support` so
    the formal matrix and this pilot cannot disagree about which rows have a
    provider-backed runtime.  The pilot additionally requires its own public
    wording to name the fixture the adapter launches; the formal catalog states
    that row observationally instead, so the requirement stays here.
    """

    tasks = _support_tasks(cells)
    failures = list(paid_runtime_support_failures(tasks, label="Phase-0"))
    for task in tasks:
        if not isinstance(task, Mapping):
            continue
        task_id = str(task.get("id") or "").strip()
        token = _PHASE0_PUBLIC_START_TOKENS.get(task_id)
        if token is None:
            continue
        if token not in str(task.get("preconditions") or "").strip().lower():
            failures.append(f"{task_id}: registered map is absent from the public Start")
    if failures:
        raise LazarusPhase0RuntimeUnsupportedError("; ".join(dict.fromkeys(failures)))


def _support_tasks(
    cells: Sequence[LazarusPhase0CellContext | Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(
        value.task if isinstance(value, LazarusPhase0CellContext) else value
        for value in cells
    )


def phase0_runtime_support_report(
    cells: Sequence[LazarusPhase0CellContext | Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a durable, provider-free support decision for a pilot population."""

    rows: list[dict[str, str]] = []
    for value in cells:
        task = value.task if isinstance(value, LazarusPhase0CellContext) else value
        task = task if isinstance(task, Mapping) else {}
        rows.append(
            {
                "task_id": str(task.get("id") or "<unknown>"),
                "benchmark": str(task.get("benchmark") or "<unknown>"),
            }
        )
    try:
        validate_lazarus_phase0_runtime_support(cells)
    except LazarusPhase0RuntimeUnsupportedError as error:
        return {
            "schema": "c1-lazarus-phase0-runtime-support-v1",
            "schema_version": 1,
            "complete": False,
            "provider_dispatch_permitted": False,
            "task_ids": [row["task_id"] for row in rows],
            "tasks": rows,
            "failures": str(error).split("; "),
        }
    return {
        "schema": "c1-lazarus-phase0-runtime-support-v1",
        "schema_version": 1,
        "complete": True,
        "provider_dispatch_permitted": True,
        "task_ids": [row["task_id"] for row in rows],
        "tasks": rows,
        "failures": [],
    }


def build_lazarus_phase0_runtime_runner(
    workspace: str | Path,
    *,
    runtime_root: str | Path = ".warptest-cache/lazarus/phase0-runtime",
    provider_timeout_s: float = C1_PROVIDER_TIMEOUT_S,
    observer_model: str = PHASE0_OBSERVER_MODEL,
) -> Callable[[LazarusPhase0CellContext], LazarusRuntimeObservation]:
    """Build the supported, provider-backed Phase-0 runtime router.

    :func:`run_lazarus_phase0` validates the complete population before it may
    invoke this callable.  The per-cell check below remains a defense in depth.
    Luanti and OpenRA retain independent arm implementations so the latter can
    prove the H2 no-checkpoint boundary for Agent-S.
    """

    return _build_runtime_router(
        workspace,
        runtime_root=runtime_root,
        provider_timeout_s=provider_timeout_s,
        observer_model=observer_model,
        guard=lambda context: validate_lazarus_phase0_runtime_support((context,)),
    )


def build_lazarus_formal_runtime_runner(
    workspace: str | Path,
    *,
    runtime_root: str | Path = ".warptest-cache/lazarus/formal-runtime",
    provider_timeout_s: float = C1_PROVIDER_TIMEOUT_S,
    observer_model: str = PHASE0_OBSERVER_MODEL,
) -> Callable[[LazarusPhase0CellContext], LazarusRuntimeObservation]:
    """Build the same router for the formal matrix.

    The arm implementations are identical; only the admission guard differs.  The
    formal population is admitted by the shared runtime registry alone, because
    the pilot's public-wording requirement describes the pilot catalog's text
    rather than a runtime capability.
    """

    return _build_runtime_router(
        workspace,
        runtime_root=runtime_root,
        provider_timeout_s=provider_timeout_s,
        observer_model=observer_model,
        guard=_formal_runtime_guard,
    )


def _formal_runtime_guard(context: LazarusPhase0CellContext) -> None:
    failures = paid_runtime_support_failures((context.task,))
    if failures:
        raise LazarusPhase0RuntimeUnsupportedError("; ".join(failures))


def _build_runtime_router(
    workspace: str | Path,
    *,
    runtime_root: str | Path,
    provider_timeout_s: float,
    observer_model: str,
    guard: Callable[[LazarusPhase0CellContext], None],
) -> Callable[[LazarusPhase0CellContext], LazarusRuntimeObservation]:
    root = Path(workspace).expanduser().resolve()
    base = Path(runtime_root).expanduser()
    base = (root / base).resolve() if not base.is_absolute() else base.resolve()
    if root != base and root not in base.parents:
        raise ValueError("Lazarus Phase-0 runtime root escapes the workspace")
    if observer_model != PHASE0_OBSERVER_MODEL:
        raise ValueError("Lazarus Phase-0 observer model does not match the manifest")
    luanti_state_runner = LazarusStateRuntimeRunner(
        synthesis=LazarusLLMStateSynthesis(
            workspace=root, provider_timeout_s=provider_timeout_s
        ),
        execution=LuantiLazarusStateExecution(
            workspace=root,
            runtime_root=base / "luanti-state",
        ),
        artifact_root=base / "state-synthesis",
    )
    openra_state_runner = LazarusStateRuntimeRunner(
        synthesis=LazarusLLMStateSynthesis(
            workspace=root, provider_timeout_s=provider_timeout_s
        ),
        execution=OpenRAHotkeyLazarusStateExecution(
            workspace=root,
            runtime_root=base / "openra-state",
        ),
        artifact_root=base / "state-synthesis",
    )
    luanti_agent_s_runner = LazarusAgentSRuntimeRunner(
        execution=LuantiLazarusAgentSExecution(
            workspace=root,
            runtime_root=base / "luanti-agent-s",
        ),
        observer_model=observer_model,
    )
    openra_agent_s_runner = LazarusAgentSRuntimeRunner(
        execution=OpenRAHotkeyLazarusAgentSExecution(
            workspace=root,
            runtime_root=base / "openra-agent-s",
            provider_timeout_s=provider_timeout_s,
        ),
        observer_model=observer_model,
    )

    def dispatch(context: LazarusPhase0CellContext) -> LazarusRuntimeObservation:
        guard(context)
        benchmark = str(context.task.get("benchmark") or "").strip().lower()
        if benchmark == "luanti" and context.key.arm == "lazarus_state":
            return luanti_state_runner(context)
        if benchmark == "openra" and context.key.arm == "lazarus_state":
            return openra_state_runner(context)
        if benchmark == "luanti" and context.key.arm == "lazarus_agent_s":
            return luanti_agent_s_runner(context)
        if benchmark == "openra" and context.key.arm == "lazarus_agent_s":
            return openra_agent_s_runner(context)
        raise ValueError(f"unknown Lazarus Phase-0 arm {context.key.arm!r}")

    return dispatch


class LuantiLazarusStateExecution:
    """Execute one Luanti state checkpoint in an isolated rendered warm session.

    The generated checkpoint supplies the Start and public transition.  Hidden
    goal/signature/invariant groups are then checked independently against the
    *same live after state*, with no version label or hidden assertion ever sent
    to the synthesis call.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_root: str | Path,
        session_factory: Any = LuantiWarmSession,
        asset_sync: Any = sync_luanti_builtin_assets,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        self._runtime_root = Path(runtime_root).expanduser().resolve()
        self._session_factory = session_factory
        self._asset_sync = asset_sync
        self._sleep_fn = sleep_fn

    def execute(
        self,
        context: LazarusPhase0CellContext,
        generated_spec: Mapping[str, Any],
        *,
        checkpoint_sha256: str,
    ) -> LazarusRuntimeExecution:
        started = time.perf_counter()
        if str(context.task.get("benchmark") or "").strip().lower() != "luanti":
            raise ValueError("Luanti runtime received a non-Luanti task")
        if not isinstance(generated_spec, Mapping) or not generated_spec:
            raise ValueError("Luanti state runtime requires a generated checkpoint")
        start_profile = _validate_luanti_public_checkpoint(context, generated_spec)
        oracle = _load_private_oracle(self._workspace, context)
        groups = _luanti_groups(oracle, start_profile)
        source = self._runtime_source(context)
        with _temporary_environment(
            "WARPTEST_LUANTI_TRUSTED_GIT_DIR", str(context.artifact.git_metadata)
        ), _temporary_environment("WARPTEST_LUANTI_SCREENSHOT_FRAME", "500"):
            sync = self._asset_sync(source)
            if not isinstance(sync, Mapping) or sync.get("status") != "synced":
                raise RuntimeError("Luanti runtime could not sync versioned builtin assets")
            output = self._runtime_output(context)
            with self._session_factory(
                source,
                output,
                render=True,
                experiment_seed=context.key.seed,
                daylight_mode="fixed_noon",
            ) as session:
                before = session.restore(
                    copy.deepcopy(dict(generated_spec)), actions=[], assertions=[]
                )
                player_restore = None
                start = None
                before_semantic_ok = False
                if _outcome_success(before):
                    player_restore = session.restore(
                        _luanti_public_player_restore(start_profile), actions=[], assertions=[]
                    )
                    if _outcome_success(player_restore):
                        start_view = _luanti_public_start_view(start_profile)
                        # The hovering player keeps drifting, so the public view is
                        # checked immediately after each restore rather than after a
                        # settle.  Re-issuing the restore is the corrective action when
                        # the client had not applied the first one yet, and the attempt
                        # bound keeps a genuinely wrong Start a failure.
                        for attempt in range(_LUANTI_PUBLIC_START_VIEW_ATTEMPTS):
                            if attempt:
                                player_restore = session.restore(
                                    _luanti_public_player_restore(start_profile),
                                    actions=[],
                                    assertions=[],
                                )
                                if not _outcome_success(player_restore):
                                    break
                            start = session.fast_restore(
                                start_view,
                                actions=[],
                                assertions=copy.deepcopy(start_view["assertions"]),
                            )
                            before_semantic_ok = _luanti_public_start_passed(start)
                            if before_semantic_ok:
                                break
                        if before_semantic_ok:
                            self._sleep_fn(_LUANTI_PUBLIC_START_SETTLE_S)
                before_path = _screenshot_path(session, "before.png")
                if not before_semantic_ok:
                    return LazarusRuntimeExecution(
                        complete=True,
                        runtime_restored=(
                            _outcome_success(before) and _outcome_success(player_restore)
                        ),
                        before_semantic_ok=False,
                        goal_checks_ok=None,
                        defect_signature_ok=None,
                        invariants_ok=None,
                        before_screenshot=before_path,
                        after_screenshot=None,
                        runtime_time_ms=int((time.perf_counter() - started) * 1000),
                        trace={
                            "schema": "c1-lazarus-luanti-execution-v1",
                            "checkpoint_sha256": checkpoint_sha256,
                            "source_clone": str(source),
                            "asset_sync": dict(sync),
                            "before_status": _outcome_status(before),
                            "player_restore_status": _outcome_status(player_restore),
                            "start_status": _outcome_status(start),
                            "capture_daylight_mode": "fixed_noon",
                            "capture_start_settle_s": _LUANTI_PUBLIC_START_SETTLE_S,
                        },
                    )
                actions = generated_spec.get("actions")
                actions = actions if isinstance(actions, list) else []
                after = session.fast_restore(
                    copy.deepcopy(dict(generated_spec)),
                    actions=copy.deepcopy(actions),
                    assertions=[],
                )
                if _outcome_success(after):
                    self._sleep_fn(_LUANTI_PUBLIC_TRANSITION_SETTLE_S)
                after_path = _screenshot_path(session, "after.png")
                goal = signature = invariants = None
                if _outcome_success(after):
                    goal = session.fast_restore(
                        _luanti_postcheck_view(oracle, groups["goal"]),
                        actions=[],
                        assertions=copy.deepcopy(groups["goal"]),
                    )
                    signature = session.fast_restore(
                        _luanti_postcheck_view(oracle, groups["defect"]),
                        actions=[],
                        assertions=copy.deepcopy(groups["defect"]),
                    )
                    invariants = session.fast_restore(
                        _luanti_postcheck_view(oracle, groups["invariants"]),
                        actions=[],
                        assertions=copy.deepcopy(groups["invariants"]),
                    )
                return LazarusRuntimeExecution(
                    complete=True,
                    runtime_restored=(
                        _outcome_success(before) and _outcome_success(player_restore)
                    ),
                    before_semantic_ok=True,
                    goal_checks_ok=_assertions_passed(goal, len(groups["goal"])),
                    defect_signature_ok=_assertions_passed(
                        signature, len(groups["defect"])
                    ),
                    invariants_ok=_assertions_passed(
                        invariants, len(groups["invariants"])
                    ),
                    before_screenshot=before_path,
                    after_screenshot=after_path,
                    runtime_time_ms=int((time.perf_counter() - started) * 1000),
                    trace={
                        "schema": "c1-lazarus-luanti-execution-v1",
                        "checkpoint_sha256": checkpoint_sha256,
                        "source_clone": str(source),
                        "asset_sync": dict(sync),
                        "before_status": _outcome_status(before),
                        "player_restore_status": _outcome_status(player_restore),
                        "start_status": _outcome_status(start),
                        "after_status": _outcome_status(after),
                        "goal_status": _outcome_status(goal),
                        "signature_status": _outcome_status(signature),
                        "invariants_status": _outcome_status(invariants),
                        "capture_daylight_mode": "fixed_noon",
                        "capture_start_settle_s": _LUANTI_PUBLIC_START_SETTLE_S,
                        "capture_transition_settle_s": (
                            _LUANTI_PUBLIC_TRANSITION_SETTLE_S
                        ),
                    },
                    p2p_restore_adapter=_luanti_p2p_restore_adapter(
                        context=context,
                        generated_spec=generated_spec,
                        session_factory=self._session_factory,
                        asset_sync=self._asset_sync,
                    ),
                )

    def _runtime_source(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "source"
        ).resolve()
        if self._runtime_root != destination and self._runtime_root not in destination.parents:
            raise ValueError("Luanti runtime clone path escapes its root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        strict_clone_tree(context.artifact.root, destination)
        return destination

    def _runtime_output(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "output"
        ).resolve()
        if self._runtime_root != destination and self._runtime_root not in destination.parents:
            raise ValueError("Luanti runtime output path escapes its root")
        destination.mkdir(parents=True, exist_ok=False)
        return destination


class LuantiLazarusAgentSExecution:
    """Run Agent-S from the visible Luanti UI and grade only trusted probes.

    Agent-S gets the two public task strings and framebuffer pixels.  It never
    receives the versioned source tree, private oracle groups, goldens, P2P
    commands, or a result field.  The coordinator validates Start and the
    three post-transition assertion groups against the same live session only
    after the policy has emitted its terminal action.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_root: str | Path,
        base_url: str = "https://api-inference.modelscope.cn/v1",
        api_key_env: str = "MODELSCOPE_API_KEY",
        temperature: float = 0.2,
        provider_timeout_s: float = 900.0,
        max_steps: int = 100,
        session_factory: Any = LuantiWarmSession,
        asset_sync: Any = sync_luanti_builtin_assets,
        runtime_factory: Any | None = None,
        phase_runner: Any | None = None,
        policy_factory: Any | None = None,
    ) -> None:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("Luanti Agent-S max_steps must be a positive integer")
        self._workspace = Path(workspace).expanduser().resolve()
        self._runtime_root = Path(runtime_root).expanduser().resolve()
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._temperature = temperature
        self._provider_timeout_s = provider_timeout_s
        self._max_steps = max_steps
        self._session_factory = session_factory
        self._asset_sync = asset_sync
        if runtime_factory is None or phase_runner is None or policy_factory is None:
            from .gui_agent import LuantiGuiRuntime, _build_policy_client, _run_phase

            runtime_factory = runtime_factory or LuantiGuiRuntime
            phase_runner = phase_runner or _run_phase
            policy_factory = policy_factory or _build_policy_client
        self._runtime_factory = runtime_factory
        self._phase_runner = phase_runner
        self._policy_factory = policy_factory

    def execute(self, context: LazarusPhase0CellContext) -> LazarusRuntimeExecution:
        started = time.perf_counter()
        if context.key.arm != "lazarus_agent_s":
            raise ValueError("Luanti Agent-S runtime received a non-Agent-S cell")
        if str(context.task.get("benchmark") or "").strip().lower() != "luanti":
            raise ValueError("Luanti Agent-S runtime received a non-Luanti task")
        start_profile = _validate_luanti_phase0_context(context)
        start_instruction = _agent_s_start_instruction(context)
        reference_steps_prompt = _agent_s_reference_steps_prompt(context)
        oracle = _load_private_oracle(self._workspace, context)
        groups = _luanti_groups(oracle, start_profile)
        source = self._runtime_source(context)
        with _temporary_environment(
            "WARPTEST_LUANTI_TRUSTED_GIT_DIR", str(context.artifact.git_metadata)
        ), _temporary_environment("WARPTEST_LUANTI_SCREENSHOT_FRAME", "500"):
            sync = self._asset_sync(source)
            if not isinstance(sync, Mapping) or sync.get("status") != "synced":
                raise RuntimeError("Luanti Agent-S runtime could not sync builtin assets")
            output = self._runtime_output(context)
            with self._session_factory(
                source,
                output,
                render=True,
                experiment_seed=context.key.seed,
                creative_mode=True,
                daylight_mode="fixed_noon",
            ) as session:
                runtime = self._runtime_factory(session)
                client = self._policy_factory(
                    policy_kind="agent_s",
                    model=context.key.model,
                    base_url=self._base_url,
                    api_key_env=self._api_key_env,
                    temperature=self._temperature,
                    grounder_model=context.key.model,
                    provider_timeout_s=self._provider_timeout_s,
                    root=self._workspace,
                )
                with _managed_agent_s_client(client) as active_client:
                    _validate_agent_s_surface(active_client)
                    phase_a = self._phase_runner(
                        runtime=runtime,
                        client=active_client,
                        instruction=start_instruction,
                        phase_dir=output / "phase-a",
                        max_steps=self._max_steps,
                        observer=None,
                        task_id=context.key.task_id,
                    )
                    before_path = _phase_screenshot(phase_a)
                    _persist_phase_actions(phase_a, output / "phase-a")
                    phase_a_trace = _phase_trace(phase_a)
                    if bool(getattr(phase_a, "incomplete", False)) or before_path is None:
                        return _incomplete_agent_s_execution(
                            started=started,
                            output=output,
                            before_path=before_path,
                            source=source,
                            sync=sync,
                            phase_a=phase_a_trace,
                            phase_b=None,
                        )
                    start_view = _luanti_public_start_view(start_profile)
                    start = session.fast_restore(
                        start_view,
                        actions=[],
                        assertions=copy.deepcopy(start_view["assertions"]),
                    )
                    before_semantic_ok = _luanti_public_start_passed(start)
                    if not before_semantic_ok:
                        return LazarusRuntimeExecution(
                            complete=True,
                            runtime_restored=True,
                            before_semantic_ok=False,
                            goal_checks_ok=None,
                            defect_signature_ok=None,
                            invariants_ok=None,
                            before_screenshot=before_path,
                            after_screenshot=None,
                            runtime_time_ms=int((time.perf_counter() - started) * 1000),
                            trace={
                                "schema": "c1-lazarus-luanti-agent-s-execution-v1",
                                "source_clone": str(source),
                                "asset_sync": dict(sync),
                                "phase_a": phase_a_trace,
                                "start_status": _outcome_status(start),
                            },
                        )
                    snapshot_fingerprint = _checkpoint_sha256(
                        {
                            "schema": "c1-lazarus-agent-s-start-snapshot-v1",
                            "identity": context.key.identity(),
                            "phase_a": phase_a_trace,
                        }
                    )
                    start_world_snapshot = session.snapshot_world(snapshot_fingerprint)
                    if not start_world_snapshot or not Path(start_world_snapshot).is_dir():
                        raise RuntimeError("Luanti Agent-S Start snapshot was not persisted")
                    start_snapshot_sha256 = tree_fingerprint(start_world_snapshot)
                    remaining = max(0, self._max_steps - _phase_steps(phase_a))
                    if not remaining:
                        return _incomplete_agent_s_execution(
                            started=started,
                            output=output,
                            before_path=before_path,
                            source=source,
                            sync=sync,
                            phase_a=phase_a_trace,
                            phase_b={
                                "steps": 0,
                                "elapsed_ms": 0,
                                "incomplete": True,
                                "action_count": 0,
                                "not_run_reason": "decision_budget_exhausted_at_start",
                            },
                        )
                    phase_b = self._phase_runner(
                        runtime=runtime,
                        client=active_client,
                        instruction=reference_steps_prompt,
                        phase_dir=output / "phase-b",
                        max_steps=remaining,
                        observer=None,
                        task_id=context.key.task_id,
                    )
                    after_path = _phase_screenshot(phase_b)
                    _persist_phase_actions(phase_b, output / "phase-b")
                    phase_b_trace = _phase_trace(phase_b)
                    phase_b_incomplete = bool(getattr(phase_b, "incomplete", False))
                    if phase_b_incomplete or after_path is None:
                        return _incomplete_agent_s_execution(
                            started=started,
                            output=output,
                            before_path=before_path,
                            source=source,
                            sync=sync,
                            phase_a=phase_a_trace,
                            phase_b=phase_b_trace,
                            after_path=after_path,
                        )
                    goal = session.fast_restore(
                        _luanti_postcheck_view(oracle, groups["goal"]),
                        actions=[],
                        assertions=copy.deepcopy(groups["goal"]),
                    )
                    signature = session.fast_restore(
                        _luanti_postcheck_view(oracle, groups["defect"]),
                        actions=[],
                        assertions=copy.deepcopy(groups["defect"]),
                    )
                    invariants = session.fast_restore(
                        _luanti_postcheck_view(oracle, groups["invariants"]),
                        actions=[],
                        assertions=copy.deepcopy(groups["invariants"]),
                    )
                    return LazarusRuntimeExecution(
                        complete=True,
                        runtime_restored=True,
                        before_semantic_ok=True,
                        goal_checks_ok=_assertions_passed(goal, len(groups["goal"])),
                        defect_signature_ok=_assertions_passed(
                            signature, len(groups["defect"])
                        ),
                        invariants_ok=_assertions_passed(
                            invariants, len(groups["invariants"])
                        ),
                        before_screenshot=before_path,
                        after_screenshot=after_path,
                        runtime_time_ms=int((time.perf_counter() - started) * 1000),
                        trace={
                            "schema": "c1-lazarus-luanti-agent-s-execution-v1",
                            "source_clone": str(source),
                            "asset_sync": dict(sync),
                            "phase_a": phase_a_trace,
                            "phase_b": phase_b_trace,
                            "start_status": _outcome_status(start),
                            "start_snapshot_sha256": start_snapshot_sha256,
                            "goal_status": _outcome_status(goal),
                            "signature_status": _outcome_status(signature),
                            "invariants_status": _outcome_status(invariants),
                        },
                        p2p_restore_adapter=_luanti_agent_s_p2p_restore_adapter(
                            context=context,
                            start_world_snapshot=start_world_snapshot,
                            start_snapshot_sha256=start_snapshot_sha256,
                            session_factory=self._session_factory,
                            asset_sync=self._asset_sync,
                        ),
                        artifact_dir=output,
                    )

    def _runtime_source(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "source"
        ).resolve()
        if self._runtime_root != destination and self._runtime_root not in destination.parents:
            raise ValueError("Luanti Agent-S runtime clone path escapes its root")
        destination.parent.mkdir(parents=True, exist_ok=True)
        strict_clone_tree(context.artifact.root, destination)
        return destination

    def _runtime_output(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "output"
        ).resolve()
        if self._runtime_root != destination and self._runtime_root not in destination.parents:
            raise ValueError("Luanti Agent-S runtime output path escapes its root")
        destination.mkdir(parents=True, exist_ok=False)
        return destination


class OpenRAHotkeyLazarusStateExecution:
    """Restore the registered OpenRA Start, then replay its real UI cycle.

    The generated specification is still the state arm's model-produced
    checkpoint and is bound to the restore request by its SHA-256.  The
    registered instrumentation performs the narrowly-scoped state materialization
    needed for this historical UI defect, then exercises cancellation and the
    normal production hotkey against the same live OpenRA session.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_root: str | Path,
        cycle_runner: Any = run_openra_pr21453_hotkey_cycle_probe,
    ) -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        self._runtime_root = Path(runtime_root).expanduser().resolve()
        self._cycle_runner = cycle_runner

    def execute(
        self,
        context: LazarusPhase0CellContext,
        generated_spec: Mapping[str, Any],
        *,
        checkpoint_sha256: str,
    ) -> LazarusRuntimeExecution:
        started = time.perf_counter()
        _validate_openra_hotkey_context(context, arm="lazarus_state")
        checkpoint_semantics = _validate_openra_hotkey_checkpoint(generated_spec)
        checkpoint_semantics_sha256 = openra_pr21453_checkpoint_semantics_sha256(
            checkpoint_semantics
        )
        if not _is_checkpoint_sha256(checkpoint_sha256):
            raise ValueError("OpenRA state runtime checkpoint SHA is invalid")
        oracle = _load_private_oracle(self._workspace, context)
        groups = _lazarus_groups(oracle, label="OpenRA state runtime")
        source = self._runtime_source(context)
        output = self._runtime_output(context)
        raw = self._cycle_runner(
            source,
            output,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_semantics=checkpoint_semantics,
        )
        return _openra_hotkey_cycle_execution(
            raw,
            started=started,
            source=source,
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_semantics=checkpoint_semantics,
            checkpoint_semantics_sha256=checkpoint_semantics_sha256,
            cycle_runner=self._cycle_runner,
            schema="c1-lazarus-openra-hotkey-state-execution-v1",
            groups=groups,
        )

    def _runtime_source(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "source"
        ).resolve()
        _assert_runtime_destination(
            self._runtime_root,
            destination,
            label="OpenRA state runtime clone",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        strict_clone_tree(context.artifact.root, destination)
        return destination

    def _runtime_output(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "output"
        ).resolve()
        _assert_runtime_destination(
            self._runtime_root,
            destination,
            label="OpenRA state runtime output",
        )
        # The real cycle runner creates this directory itself.  Keeping it
        # absent here lets a fake runner in the regression seam prove the same
        # ownership contract.
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination


class OpenRAHotkeyLazarusAgentSExecution:
    """Run Agent-S through the public OpenRA hotkey phases without a checkpoint.

    Phase A begins at the raw, non-placement-ready match.  The model sees only
    public text and framebuffer pixels, so it must use the ordinary Buildings
    hotkey to reach Start.  The state restore environment is explicitly removed
    for the complete session.  Phase B then has to cancel and re-run the public
    hotkey cycle before the trusted audit classifies the resulting live state.
    """

    def __init__(
        self,
        *,
        workspace: str | Path,
        runtime_root: str | Path,
        base_url: str = "https://api-inference.modelscope.cn/v1",
        api_key_env: str = "MODELSCOPE_API_KEY",
        temperature: float = 0.2,
        provider_timeout_s: float = C1_PROVIDER_TIMEOUT_S,
        max_steps: int = 100,
        session_factory: Any = OpenRAGameplaySession,
        phase_runner: Any | None = None,
        policy_factory: Any | None = None,
    ) -> None:
        if isinstance(max_steps, bool) or not isinstance(max_steps, int) or max_steps < 1:
            raise ValueError("OpenRA Agent-S max_steps must be a positive integer")
        self._workspace = Path(workspace).expanduser().resolve()
        self._runtime_root = Path(runtime_root).expanduser().resolve()
        self._base_url = base_url
        self._api_key_env = api_key_env
        self._temperature = temperature
        self._provider_timeout_s = provider_timeout_s
        self._max_steps = max_steps
        self._session_factory = session_factory
        if phase_runner is None or policy_factory is None:
            from .gui_agent import _build_policy_client, _run_phase

            phase_runner = phase_runner or _run_phase
            policy_factory = policy_factory or _build_policy_client
        self._phase_runner = phase_runner
        self._policy_factory = policy_factory

    def execute(self, context: LazarusPhase0CellContext) -> LazarusRuntimeExecution:
        started = time.perf_counter()
        _validate_openra_hotkey_context(context, arm="lazarus_agent_s")
        start_instruction = _agent_s_start_instruction(context)
        reference_steps_prompt = _agent_s_reference_steps_prompt(context)
        oracle = _load_private_oracle(self._workspace, context)
        groups = _lazarus_groups(oracle, label="OpenRA Agent-S runtime")
        source = self._runtime_source(context)
        output = self._runtime_output(context)
        audit_path = output / "openra-hotkey-ready-building-audit.json"
        spec = openra_pr21453_hotkey_probe("after")

        with _temporary_environment(OPENRA_HOTKEY_AUDIT_ENV, str(audit_path)):
            # A parent process may have used the state arm immediately before
            # this cell.  Do not let that environment leak into the from-start
            # baseline, even transiently while the game process is spawned.
            with _without_environment(OPENRA_HOTKEY_STATE_ENV):
                with self._session_factory(
                    source,
                    output,
                    spec=spec,
                    experiment_seed=context.key.seed,
                    bootstrap_timeout_s=120.0,
                ) as session:
                    session.start()
                    initial = _wait_for_hotkey_audit(
                        audit_path,
                        deadline=time.monotonic() + 120.0,
                    )
                    _raise_if_agent_s_received_openra_state(initial)
                    client = self._policy_factory(
                        policy_kind="agent_s",
                        model=context.key.model,
                        base_url=self._base_url,
                        api_key_env=self._api_key_env,
                        temperature=self._temperature,
                        grounder_model=context.key.model,
                        provider_timeout_s=self._provider_timeout_s,
                        root=self._workspace,
                    )
                    with _managed_agent_s_client(client) as active_client:
                        _validate_agent_s_surface(active_client)
                        phase_a = self._phase_runner(
                            runtime=session,
                            client=active_client,
                            instruction=start_instruction,
                            phase_dir=output / "phase-a",
                            max_steps=self._max_steps,
                            observer=None,
                            task_id=context.key.task_id,
                        )
                        before_path = _phase_screenshot(phase_a)
                        _persist_phase_actions(phase_a, output / "phase-a")
                        phase_a_trace = _phase_trace(phase_a)
                        if (
                            bool(getattr(phase_a, "incomplete", False))
                            or before_path is None
                        ):
                            return _incomplete_openra_agent_s_execution(
                                started=started,
                                output=output,
                                before_path=before_path,
                                source=source,
                                phase_a=phase_a_trace,
                                phase_b=None,
                            )
                        if not _phase_has_openra_start_hotkey(phase_a):
                            return _incomplete_openra_agent_s_execution(
                                started=started,
                                output=output,
                                before_path=before_path,
                                source=source,
                                phase_a={
                                    **phase_a_trace,
                                    "missing_public_buildings_hotkey": True,
                                },
                                phase_b=None,
                            )
                        before_raw = _wait_for_hotkey_audit(
                            audit_path,
                            deadline=time.monotonic() + 30.0,
                            minimum_world_tick=int(initial["world_tick"]) + 1,
                        )
                        _raise_if_agent_s_received_openra_state(before_raw)
                        before_audit = evaluate_openra_pr21453_hotkey_audit(before_raw)
                        before_semantic_ok = (
                            before_audit.complete
                            and before_raw.get("placement_active") is True
                        )
                        if not before_semantic_ok:
                            return LazarusRuntimeExecution(
                                complete=True,
                                runtime_restored=True,
                                before_semantic_ok=False,
                                goal_checks_ok=None,
                                defect_signature_ok=None,
                                invariants_ok=None,
                                before_screenshot=before_path,
                                after_screenshot=None,
                                runtime_time_ms=int(
                                    (time.perf_counter() - started) * 1000
                                ),
                                trace={
                                    "schema": "c1-lazarus-openra-hotkey-agent-s-execution-v1",
                                    "source_clone": str(source),
                                    "phase_a": phase_a_trace,
                                    "before_audit_complete": before_audit.complete,
                                    "before_placement_active": False,
                                },
                                artifact_dir=output,
                            )
                        remaining = max(0, self._max_steps - _phase_steps(phase_a))
                        if not remaining:
                            return _incomplete_openra_agent_s_execution(
                                started=started,
                                output=output,
                                before_path=before_path,
                                source=source,
                                phase_a=phase_a_trace,
                                phase_b={
                                    "steps": 0,
                                    "elapsed_ms": 0,
                                    "incomplete": True,
                                    "action_count": 0,
                                    "not_run_reason": "decision_budget_exhausted_at_start",
                                },
                            )
                        phase_b = self._phase_runner(
                            runtime=session,
                            client=active_client,
                            instruction=reference_steps_prompt,
                            phase_dir=output / "phase-b",
                            max_steps=remaining,
                            observer=None,
                            task_id=context.key.task_id,
                        )
                        after_path = _phase_screenshot(phase_b)
                        _persist_phase_actions(phase_b, output / "phase-b")
                        phase_b_trace = _phase_trace(phase_b)
                        if (
                            bool(getattr(phase_b, "incomplete", False))
                            or after_path is None
                        ):
                            return _incomplete_openra_agent_s_execution(
                                started=started,
                                output=output,
                                before_path=before_path,
                                after_path=after_path,
                                source=source,
                                phase_a=phase_a_trace,
                                phase_b=phase_b_trace,
                            )
                        if not _phase_has_openra_hotkey_cycle(phase_b):
                            return _incomplete_openra_agent_s_execution(
                                started=started,
                                output=output,
                                before_path=before_path,
                                after_path=after_path,
                                source=source,
                                phase_a=phase_a_trace,
                                phase_b={
                                    **phase_b_trace,
                                    "missing_public_hotkey_cycle": True,
                                },
                            )
                        after_raw = _wait_for_hotkey_audit(
                            audit_path,
                            deadline=time.monotonic() + 30.0,
                            minimum_world_tick=int(before_raw["world_tick"]) + 1,
                        )
                        _raise_if_agent_s_received_openra_state(after_raw)
                        after_audit = evaluate_openra_pr21453_hotkey_audit(after_raw)
                        if after_raw.get("placement_cancel_observed") is not True:
                            return _incomplete_openra_agent_s_execution(
                                started=started,
                                output=output,
                                before_path=before_path,
                                after_path=after_path,
                                source=source,
                                phase_a=phase_a_trace,
                                phase_b={
                                    **phase_b_trace,
                                    "placement_cancellation_not_observed": True,
                                },
                            )
                        group_results = _evaluate_openra_lazarus_groups(
                            groups,
                            after_raw,
                        )
                        return LazarusRuntimeExecution(
                            complete=True,
                            runtime_restored=True,
                            before_semantic_ok=True,
                            goal_checks_ok=group_results["goal"],
                            defect_signature_ok=group_results["defect"],
                            invariants_ok=group_results["invariants"],
                            before_screenshot=before_path,
                            after_screenshot=after_path,
                            runtime_time_ms=int(
                                (time.perf_counter() - started) * 1000
                            ),
                            trace={
                                "schema": "c1-lazarus-openra-hotkey-agent-s-execution-v1",
                                "source_clone": str(source),
                                "phase_a": phase_a_trace,
                                "phase_b": phase_b_trace,
                                "before_audit_complete": before_audit.complete,
                                "after_audit_complete": after_audit.complete,
                                "state_environment_absent": True,
                                "registered_group_results": group_results,
                            },
                            p2p_restore_adapter=_openra_agent_s_p2p_restore_adapter(),
                            artifact_dir=output,
                        )

    def _runtime_source(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "source"
        ).resolve()
        _assert_runtime_destination(
            self._runtime_root,
            destination,
            label="OpenRA Agent-S runtime clone",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        strict_clone_tree(context.artifact.root, destination)
        return destination

    def _runtime_output(self, context: LazarusPhase0CellContext) -> Path:
        destination = (
            self._runtime_root / context.key.invocation_id() / "output"
        ).resolve()
        _assert_runtime_destination(
            self._runtime_root,
            destination,
            label="OpenRA Agent-S runtime output",
        )
        destination.mkdir(parents=True, exist_ok=False)
        return destination


def _load_private_oracle(
    workspace: Path, context: LazarusPhase0CellContext
) -> dict[str, Any]:
    value = context.task.get("oracle_spec")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Lazarus runtime task has no private oracle path")
    path = (workspace / value).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError("Lazarus runtime oracle path escapes the workspace")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError("Lazarus runtime cannot load its private oracle") from error
    if not isinstance(loaded, Mapping):
        raise ValueError("Lazarus runtime private oracle is not a mapping")
    return copy.deepcopy(dict(loaded))


def _luanti_groups(
    oracle: Mapping[str, Any], start: LuantiPhase0Start
) -> dict[str, list[dict[str, Any]]]:
    """Load the hidden groups and express them as live-world assertions."""

    groups = _lazarus_groups(oracle, label="Luanti runtime")
    return {
        label: luanti_live_assertions(
            start, checks, label=f"Luanti runtime {label} group"
        )
        for label, checks in groups.items()
    }


def _lazarus_groups(
    oracle: Mapping[str, Any], *, label: str
) -> dict[str, list[dict[str, Any]]]:
    evaluation = oracle.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, Mapping) else {}
    lazarus = evaluation.get("lazarus")
    if not isinstance(lazarus, Mapping):
        raise ValueError(f"{label} oracle has no Lazarus group contract")
    source_keys = {
        "goal": "goal_checks",
        "defect": "defect_signature",
        "invariants": "invariant_checks",
    }
    groups: dict[str, list[dict[str, Any]]] = {}
    for target, source in source_keys.items():
        checks = lazarus.get(source)
        if not isinstance(checks, list) or not checks or not all(
            isinstance(check, Mapping) for check in checks
        ):
            raise ValueError(f"{label} oracle group {source} is invalid")
        groups[target] = [copy.deepcopy(dict(check)) for check in checks]
    return groups


def _evaluate_openra_lazarus_groups(
    groups: Mapping[str, list[dict[str, Any]]], audit: Mapping[str, Any]
) -> dict[str, bool]:
    """Project every registered OpenRA Lazarus check over one live audit.

    The historical helper's ``goal_checks_ok`` convenience flag describes only
    the PR #21453 default.  Phase-0 must instead evaluate all hidden groups,
    including any future invariant, or fail closed when an unsupported check is
    introduced.  This keeps the runtime's semantic result coupled to the oracle
    rather than to a fixture-specific boolean.
    """

    result: dict[str, bool] = {}
    for label in ("goal", "defect", "invariants"):
        checks = groups.get(label)
        if not isinstance(checks, list) or not checks:
            result[label] = False
            continue
        result[label] = all(_evaluate_openra_lazarus_check(check, audit) for check in checks)
    return result


def _evaluate_openra_lazarus_check(
    check: Mapping[str, Any], audit: Mapping[str, Any]
) -> bool:
    kind = str(check.get("type") or "").strip()
    if kind == "openra_lazarus_hotkey_audit":
        field = str(check.get("field") or "").strip()
        expected = check.get("expected")
        if type(expected) is not bool:
            return False
        values: dict[str, bool] = {
            "placement_active": audit.get("placement_active") is True,
            "placement_cancel_observed": audit.get("placement_cancel_observed")
            is True,
            "ready_hidden_building": (
                evaluate_openra_pr21453_hotkey_audit(audit).complete
                and audit.get("queue_ready") is True
                and audit.get("target_hidden") is True
            ),
        }
        actual = values.get(field)
        return actual is not None and actual is expected
    if kind == "no_openra_gameplay_probe_errors":
        errors = audit.get("probe_errors")
        return (
            isinstance(errors, list)
            and not errors
            and not str(audit.get("state_restore_error") or "")
        )
    return False


def _validate_openra_hotkey_context(
    context: LazarusPhase0CellContext, *, arm: str
) -> None:
    if context.key.arm != arm:
        raise ValueError(f"OpenRA hotkey runtime received a non-{arm} cell")
    if context.key.task_id != OPENRA_HOTKEY_PHASE0_TASK_ID:
        raise ValueError("OpenRA hotkey runtime received an unregistered task")
    if str(context.task.get("benchmark") or "").strip().lower() != "openra":
        raise ValueError("OpenRA hotkey runtime received a non-OpenRA task")
    if context.defect_locus != "on_path":
        raise ValueError("OpenRA hotkey runtime requires an on_path task")


def _validate_openra_hotkey_checkpoint(
    generated_spec: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(generated_spec, Mapping):
        raise ValueError("OpenRA state runtime checkpoint must be a mapping")
    target = generated_spec.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "openra_map":
        raise ValueError("OpenRA state runtime checkpoint must target an OpenRA map")
    checkpoint = generated_spec.get("checkpoint")
    allowed = checkpoint.get("allowed_strategies") if isinstance(checkpoint, Mapping) else None
    if not isinstance(allowed, list) or "openra_gameplay_production_validate" not in allowed:
        raise ValueError(
            "OpenRA state runtime checkpoint lacks the registered gameplay strategy"
        )
    return canonicalize_openra_pr21453_checkpoint_semantics(generated_spec)


def _is_checkpoint_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _assert_runtime_destination(root: Path, destination: Path, *, label: str) -> None:
    base = root.expanduser().resolve()
    if base != destination and base not in destination.parents:
        raise ValueError(f"{label} path escapes its root")


def _openra_hotkey_cycle_execution(
    raw: Any,
    *,
    started: float,
    source: Path,
    checkpoint_sha256: str,
    checkpoint_semantics: Mapping[str, Any],
    checkpoint_semantics_sha256: str,
    cycle_runner: Any,
    schema: str,
    groups: Mapping[str, list[dict[str, Any]]],
) -> LazarusRuntimeExecution:
    """Translate a trusted cycle probe into raw Lazarus execution evidence."""

    runtime_time_ms = int((time.perf_counter() - started) * 1000)
    if not isinstance(raw, Mapping):
        return _incomplete_openra_state_execution(
            runtime_time_ms=runtime_time_ms,
            source=source,
            checkpoint_sha256=checkpoint_sha256,
            schema=schema,
            cycle_status="invalid_summary",
        )
    before = raw.get("before")
    after = raw.get("after")
    before_path = _summary_existing_path(raw.get("before_path"))
    after_path = _summary_existing_path(raw.get("after_path"))
    before_ready = (
        isinstance(before, Mapping)
        and before.get("executed") is True
        and before.get("ready") is True
        and before.get("state_restore_applied") is True
        and before.get("state_restore_spec_sha256") == checkpoint_semantics_sha256
    )
    after_fields = (
        isinstance(after, Mapping)
        and after.get("executed") is True
        and isinstance(after.get("raw_audit"), Mapping)
    )
    if not before_ready or not after_fields or before_path is None or after_path is None:
        return _incomplete_openra_state_execution(
            runtime_time_ms=runtime_time_ms,
            source=source,
            checkpoint_sha256=checkpoint_sha256,
            schema=schema,
            cycle_status=str(raw.get("status") or "incomplete"),
            before_path=before_path,
            after_path=after_path,
        )
    assert isinstance(after, Mapping)
    raw_audit = after["raw_audit"]
    assert isinstance(raw_audit, Mapping)
    group_results = _evaluate_openra_lazarus_groups(groups, raw_audit)
    return LazarusRuntimeExecution(
        complete=True,
        runtime_restored=True,
        before_semantic_ok=True,
        goal_checks_ok=group_results["goal"],
        defect_signature_ok=group_results["defect"],
        invariants_ok=group_results["invariants"],
        before_screenshot=before_path,
        after_screenshot=after_path,
        runtime_time_ms=runtime_time_ms,
        trace={
            "schema": schema,
            "source_clone": str(source),
            "checkpoint_sha256": checkpoint_sha256,
            "checkpoint_semantics_sha256": checkpoint_semantics_sha256,
            "cycle_status": str(raw.get("status") or "unknown"),
            "state_restore_applied": True,
            "registered_group_results": group_results,
        },
        p2p_restore_adapter=_openra_state_p2p_restore_adapter(
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_semantics=checkpoint_semantics,
            cycle_runner=cycle_runner,
        ),
    )


def _incomplete_openra_state_execution(
    *,
    runtime_time_ms: int,
    source: Path,
    checkpoint_sha256: str,
    schema: str,
    cycle_status: str,
    before_path: Path | None = None,
    after_path: Path | None = None,
) -> LazarusRuntimeExecution:
    return LazarusRuntimeExecution(
        complete=False,
        runtime_restored=False,
        before_semantic_ok=None,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        before_screenshot=before_path,
        after_screenshot=after_path,
        runtime_time_ms=runtime_time_ms,
        trace={
            "schema": schema,
            "source_clone": str(source),
            "checkpoint_sha256": checkpoint_sha256,
            "cycle_status": cycle_status,
        },
    )


def _summary_existing_path(value: Any) -> Path | None:
    if not isinstance(value, (str, Path)) or not str(value):
        return None
    path = Path(value).expanduser().resolve()
    return path if path.is_file() else None


def _raise_if_agent_s_received_openra_state(audit: Mapping[str, Any]) -> None:
    if (
        audit.get("state_restore_requested") is True
        or audit.get("state_restore_applied") is True
        or str(audit.get("state_checkpoint_sha256") or "")
    ):
        raise RuntimeError("OpenRA Agent-S runtime received a state checkpoint")


def _openra_phase_actions(phase: Any) -> list[Mapping[str, Any]]:
    values = getattr(phase, "actions", ())
    if not isinstance(values, (list, tuple)):
        return []
    actions: list[Mapping[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        structured = value.get("structured_actions")
        if isinstance(structured, list) and all(
            isinstance(item, Mapping) for item in structured
        ):
            actions.extend(item for item in structured if isinstance(item, Mapping))
        else:
            actions.append(value)
    return actions


def _phase_has_openra_start_hotkey(phase: Any) -> bool:
    """Require the registered visible Buildings hotkey in Agent-S Phase A."""

    return any(
        str(action.get("kind") or "").strip().lower() == "key"
        and str(action.get("key") or "").strip().lower() == "e"
        for action in _openra_phase_actions(phase)
    )


def _phase_has_openra_hotkey_cycle(phase: Any) -> bool:
    """Require evidence of the public cancel-then-hotkey transition in Phase B."""

    saw_cancel = False
    for action in _openra_phase_actions(phase):
        kind = str(action.get("kind") or "").strip().lower()
        if kind == "click" and str(action.get("button") or "left").lower() == "right":
            saw_cancel = True
        elif saw_cancel and kind == "key" and str(action.get("key") or "").lower() == "e":
            return True
    return False


def _incomplete_openra_agent_s_execution(
    *,
    started: float,
    output: Path,
    before_path: Path | None,
    source: Path,
    phase_a: Mapping[str, Any],
    phase_b: Mapping[str, Any] | None,
    after_path: Path | None = None,
) -> LazarusRuntimeExecution:
    trace: dict[str, Any] = {
        "schema": "c1-lazarus-openra-hotkey-agent-s-execution-v1",
        "source_clone": str(source),
        "phase_a": dict(phase_a),
        "runtime_status": "incomplete",
        "state_environment_absent": True,
    }
    if phase_b is not None:
        trace["phase_b"] = dict(phase_b)
    return LazarusRuntimeExecution(
        complete=False,
        runtime_restored=True,
        before_semantic_ok=None,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        before_screenshot=before_path,
        after_screenshot=after_path,
        runtime_time_ms=int((time.perf_counter() - started) * 1000),
        trace=trace,
        artifact_dir=output,
    )


def _validate_luanti_phase0_context(
    context: LazarusPhase0CellContext,
) -> LuantiPhase0Start:
    """Resolve the registered public Start, refusing any unregistered task."""

    start = luanti_start_for_task_id(context.key.task_id)
    if start is None:
        raise ValueError("Luanti Phase-0 runtime received an unregistered task")
    if str(context.task.get("id") or "") != start.task_id:
        raise ValueError("Luanti Phase-0 task identity drift")
    return start


def _luanti_public_start_view(start: LuantiPhase0Start) -> dict[str, Any]:
    validations = [
        {"path": path, "expected": expected}
        for path, expected in sorted(start.validations.items())
    ]
    return {
        "name": start.view_name,
        "benchmark": "luanti",
        "target": {"kind": "world_loaded", "validations": validations},
        "validations": copy.deepcopy(validations),
        "actions": [],
        "assertions": [luanti_pr11919_public_view_assertion()],
    }


def _luanti_public_player_restore(start: LuantiPhase0Start) -> dict[str, Any]:
    return {
        "name": "Restore the public Luanti player view",
        "benchmark": "luanti",
        "target": {
            "kind": "synthesized_world",
            "player": copy.deepcopy(dict(start.player)),
        },
        "actions": [],
        "assertions": [],
    }


def _validate_luanti_public_checkpoint(
    context: LazarusPhase0CellContext, generated_spec: Mapping[str, Any]
) -> LuantiPhase0Start:
    """Accept a checkpoint only if it reproduces the task's registered Start."""

    start = _validate_luanti_phase0_context(context)
    target = generated_spec.get("target")
    if not isinstance(target, Mapping) or target.get("kind") != "synthesized_world":
        raise ValueError("Luanti checkpoint does not target the public synthesized Start")
    nodes = target.get("nodes")
    nodes_by_position = {
        (
            (node.get("pos") or {}).get("x"),
            (node.get("pos") or {}).get("y"),
            (node.get("pos") or {}).get("z"),
        ): node.get("node")
        for node in nodes or []
        if isinstance(node, Mapping) and isinstance(node.get("pos"), Mapping)
    }
    validations = target.get("validations")
    validations_by_path = {
        validation.get("path"): validation.get("expected")
        for validation in validations or []
        if isinstance(validation, Mapping)
    }
    if nodes_by_position != dict(start.nodes):
        raise ValueError("Luanti checkpoint nodes drift from the public Start")
    if validations_by_path != dict(start.validations):
        raise ValueError("Luanti checkpoint validations drift from the public Start")
    if set(target) != {"kind", "nodes", "player", "validations"}:
        raise ValueError("Luanti checkpoint target contains undeclared Start fields")
    if target.get("player") != dict(start.player):
        raise ValueError("Luanti checkpoint player view drifts from the public Start")
    if generated_spec.get("actions") != [dict(start.action)]:
        raise ValueError("Luanti checkpoint transition drifts from the public action")
    return start


def _luanti_p2p_restore_adapter(
    *,
    context: LazarusPhase0CellContext,
    generated_spec: Mapping[str, Any],
    session_factory: Any,
    asset_sync: Any,
) -> Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]]:
    """Create a real isolated Luanti Start-restoration adapter for P2P.

    The callback is retained only in process memory until the coordinator forks
    the disposable upstream-test clone.  It consumes the materialized receipt,
    starts the versioned engine in that clone, restores the same generated
    checkpoint and validates the registered Start view before P2P begins.
    """

    checkpoint = copy.deepcopy(dict(generated_spec))
    start_profile = _validate_luanti_phase0_context(context)
    start_view = _luanti_public_start_view(start_profile)
    seed = context.key.seed

    def restore(
        clone: Path, receipt_path: Path, receipt: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        _consume_p2p_restore_receipt(receipt_path, receipt)
        output = clone / ".warptest" / "lazarus" / "p2p-state-restore"
        sync = asset_sync(clone)
        if not isinstance(sync, Mapping) or sync.get("status") != "synced":
            raise RuntimeError("Luanti P2P clone could not sync versioned builtin assets")
        with session_factory(
            clone,
            output,
            render=True,
            experiment_seed=seed,
            daylight_mode="fixed_noon",
        ) as session:
            synthesized = session.restore(
                copy.deepcopy(checkpoint), actions=[], assertions=[]
            )
            player_restore = None
            start = None
            if _outcome_success(synthesized):
                player_restore = session.restore(
                    _luanti_public_player_restore(start_profile), actions=[], assertions=[]
                )
                if _outcome_success(player_restore):
                    start = session.fast_restore(
                        start_view,
                        actions=[],
                        assertions=copy.deepcopy(start_view["assertions"]),
                    )
            applied = (
                _outcome_success(synthesized)
                and _outcome_success(player_restore)
                and _luanti_public_start_passed(start)
            )
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "luanti-isolated-checkpoint-restore-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "asset_sync": str(sync.get("status") or "unknown"),
            "synthesis_status": _outcome_status(synthesized),
            "player_restore_status": _outcome_status(player_restore),
            "start_status": _outcome_status(start),
        }

    return restore


def _luanti_agent_s_p2p_restore_adapter(
    *,
    context: LazarusPhase0CellContext,
    start_world_snapshot: str | Path,
    start_snapshot_sha256: str,
    session_factory: Any,
    asset_sync: Any,
) -> Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]]:
    """Restore Agent-S's achieved Start snapshot in an isolated clone."""

    start_profile = _validate_luanti_phase0_context(context)
    seed = context.key.seed
    start_view = _luanti_public_start_view(start_profile)
    snapshot = Path(start_world_snapshot).expanduser().resolve()
    if not snapshot.is_dir():
        raise ValueError("Luanti Agent-S Start snapshot is missing")
    if tree_fingerprint(snapshot) != start_snapshot_sha256:
        raise ValueError("Luanti Agent-S Start snapshot fingerprint drift")

    def restore(
        clone: Path, receipt_path: Path, receipt: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        _consume_p2p_restore_receipt(receipt_path, receipt)
        if tree_fingerprint(snapshot) != start_snapshot_sha256:
            raise RuntimeError("Luanti Agent-S Start snapshot changed before P2P restore")
        output = clone / ".warptest" / "lazarus" / "p2p-public-start"
        sync = asset_sync(clone)
        if not isinstance(sync, Mapping) or sync.get("status") != "synced":
            raise RuntimeError("Luanti Agent-S P2P clone could not sync builtin assets")
        with session_factory(
            clone,
            output,
            render=True,
            experiment_seed=seed,
            daylight_mode="fixed_noon",
            initial_world_snapshot=snapshot,
        ) as session:
            player_restore = session.restore(
                _luanti_public_player_restore(start_profile), actions=[], assertions=[]
            )
            start = session.fast_restore(
                copy.deepcopy(start_view),
                actions=[],
                assertions=copy.deepcopy(start_view["assertions"]),
            )
            applied = _outcome_success(player_restore) and _luanti_public_start_passed(
                start
            )
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "luanti-agent-s-start-snapshot-restore-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "asset_sync": str(sync.get("status") or "unknown"),
            "start_snapshot_sha256": start_snapshot_sha256,
            "player_restore_status": _outcome_status(player_restore),
            "start_status": _outcome_status(start),
            "full_restore_used": True,
        }

    return restore


def _consume_p2p_restore_receipt(
    receipt_path: Path, receipt: Mapping[str, Any]
) -> None:
    """Require a backend adapter to read the clone-local checkpoint receipt."""

    encoded = receipt_path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != receipt.get("payload_sha256"):
        raise ValueError("P2P restore receipt hash drift")
    try:
        loaded = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("P2P restore receipt is unreadable") from error
    if not isinstance(loaded, Mapping):
        raise ValueError("P2P restore receipt is not a mapping")
    for name in ("checkpoint_sha256", "checkpoint_kind", "runtime_restored"):
        if loaded.get(name) != receipt.get(name):
            raise ValueError(f"P2P restore receipt {name} drift")


def _openra_state_p2p_restore_adapter(
    *,
    checkpoint_sha256: str,
    checkpoint_semantics: Mapping[str, Any],
    cycle_runner: Any = run_openra_pr21453_hotkey_cycle_probe,
) -> Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]]:
    """Restore the state-arm OpenRA Start inside its isolated P2P clone."""

    def restore(
        clone: Path, receipt_path: Path, receipt: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        _consume_p2p_restore_receipt(receipt_path, receipt)
        summary = cycle_runner(
            clone,
            clone / ".warptest" / "lazarus" / "p2p-state-restore",
            checkpoint_sha256=checkpoint_sha256,
            checkpoint_semantics=copy.deepcopy(dict(checkpoint_semantics)),
            timeout_s=120.0,
        )
        before = summary.get("before") if isinstance(summary, Mapping) else None
        applied = (
            isinstance(before, Mapping)
            and before.get("ready") is True
            and before.get("state_restore_applied") is True
            and before.get("state_restore_spec_sha256")
            == openra_pr21453_checkpoint_semantics_sha256(checkpoint_semantics)
        )
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "openra-hotkey-isolated-state-restore-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "probe_status": str(summary.get("status") or "invalid")
            if isinstance(summary, Mapping)
            else "invalid",
        }

    return restore


def _openra_agent_s_p2p_restore_adapter(
) -> Callable[[Path, Path, Mapping[str, Any]], Mapping[str, Any]]:
    """Recreate Agent-S's public Start without exposing a state injection.

    Agent-S has no reusable private checkpoint.  Its P2P clone therefore
    drives the same ordinary visible production hotkey from the raw match and
    accepts the restoration only if the live OpenRA audit reaches Start with
    the state-restore environment absent.
    """

    def restore(
        clone: Path, receipt_path: Path, receipt: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        _consume_p2p_restore_receipt(receipt_path, receipt)
        with _without_environment(OPENRA_HOTKEY_STATE_ENV):
            summary = run_openra_pr21453_hotkey_probe(
                clone,
                clone / ".warptest" / "lazarus" / "p2p-agent-s-start",
                timeout_s=120.0,
            )
        after = summary.get("after") if isinstance(summary, Mapping) else None
        applied = (
            isinstance(after, Mapping)
            and after.get("goal_checks_ok") is True
            and after.get("defect_signature_ok") is False
        )
        return {
            "schema_version": 1,
            "state_restore_applied": applied,
            "adapter": "openra-hotkey-public-agent-s-start-v1",
            "checkpoint_sha256": receipt.get("checkpoint_sha256"),
            "checkpoint_kind": receipt.get("checkpoint_kind"),
            "receipt_sha256": receipt.get("payload_sha256"),
            "probe_status": str(summary.get("status") or "invalid")
            if isinstance(summary, Mapping)
            else "invalid",
        }

    return restore


def _luanti_postcheck_view(
    oracle: Mapping[str, Any], assertions: list[dict[str, Any]]
) -> dict[str, Any]:
    result = copy.deepcopy(dict(oracle))
    result["target"] = {"kind": "world_loaded"}
    result["actions"] = []
    result["assertions"] = copy.deepcopy(assertions)
    evaluation = result.get("evaluation")
    if isinstance(evaluation, dict):
        semantic = evaluation.get("semantic")
        if isinstance(semantic, dict):
            semantic["assertions"] = copy.deepcopy(assertions)
    return result


def _outcome_status(value: Any) -> str:
    return str(getattr(value, "status", "not_executed") or "not_executed")


def _outcome_success(value: Any) -> bool:
    return _outcome_status(value) == "success"


def _target_validates(value: Any) -> bool:
    if not _outcome_success(value):
        return False
    checks = getattr(value, "checks", None)
    target = [
        check
        for check in checks or []
        if isinstance(check, Mapping)
        and str(check.get("name") or "").startswith("target.validate.")
    ]
    return bool(target) and all(check.get("status") == "success" for check in target)


def _luanti_public_start_passed(value: Any) -> bool:
    return _target_validates(value) and _assertions_passed(value, 1)


def _assertions_passed(value: Any, expected_count: int) -> bool:
    if not _outcome_success(value) or expected_count < 1:
        return False
    checks = getattr(value, "checks", None)
    assertions = [
        check
        for check in checks or []
        if isinstance(check, Mapping)
        and str(check.get("name") or "").startswith("assertion[")
    ]
    return len(assertions) == expected_count and all(
        check.get("status") == "success" for check in assertions
    )


def _screenshot_path(session: Any, name: str) -> Path | None:
    value = session.screenshot(name)
    path = Path(str(value)).expanduser().resolve() if value else None
    return path if path is not None and path.is_file() else None


def _agent_s_start_instruction(context: LazarusPhase0CellContext) -> str:
    text = (
        "Use only the visible application UI to satisfy these public "
        "preconditions. Inspect the resulting frame, then emit done.\n\n"
        "Preconditions:\n"
        + _public_task_text(context, "preconditions")
    )
    _assert_public_instruction(context, text)
    return text


def _agent_s_reference_steps_prompt(context: LazarusPhase0CellContext) -> str:
    text = (
        "Use only the visible application UI. Complete the following public "
        "reference steps in order. Inspect the resulting frame after each "
        "step, and emit done only after the listed steps have been attempted.\n\n"
        "Reference steps:\n"
        + _public_task_text(context, "reference_steps")
    )
    _assert_public_instruction(context, text)
    return text


def _public_task_text(context: LazarusPhase0CellContext, field: str) -> str:
    value = context.task.get(field)
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"Lazarus Agent-S task public field {field} is missing")
    return text


def _assert_public_instruction(context: LazarusPhase0CellContext, text: str) -> None:
    normalized = text.casefold()
    for token in context.agent_source.forbidden_tokens:
        candidate = str(token).strip()
        if candidate and candidate.casefold() in normalized:
            raise ValueError("Lazarus Agent-S instruction contains a private source token")


@contextmanager
def _managed_agent_s_client(client: Any) -> Iterator[Any]:
    enter = getattr(client, "__enter__", None)
    if callable(enter):
        with client as active:
            yield active
        return
    try:
        yield client
    finally:
        close = getattr(client, "close", None)
        if callable(close):
            close()


def _validate_agent_s_surface(client: Any) -> None:
    metadata = getattr(client, "metadata", None)
    if not isinstance(metadata, Mapping):
        raise ValueError("Agent-S client did not attest its execution surface")
    if metadata.get("local_code_enabled") is not False:
        raise ValueError("Agent-S local-code environment must remain disabled")
    if metadata.get("worker_observation_mode") != "screenshot":
        raise ValueError("Agent-S must use screenshot-only observations")


def _phase_screenshot(phase: Any) -> Path | None:
    value = getattr(phase, "screenshot", None)
    path = Path(str(value)).expanduser().resolve() if value else None
    return path if path is not None and path.is_file() else None


def _phase_steps(phase: Any) -> int:
    value = getattr(phase, "steps", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _phase_trace(phase: Any) -> dict[str, Any]:
    actions = getattr(phase, "actions", ())
    action_count = len(actions) if isinstance(actions, (list, tuple)) else 0
    elapsed = getattr(phase, "elapsed_ms", 0)
    if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)):
        elapsed = 0
    return {
        "steps": _phase_steps(phase),
        "elapsed_ms": max(0, int(elapsed)),
        "incomplete": bool(getattr(phase, "incomplete", False)),
        "action_count": action_count,
    }


PHASE_ACTIONS_FILENAME = "actions.json"
COST_LEDGER_FILENAME = "cost-ledger.json"


def _write_trajectory_artifact(path: Path, payload: Mapping[str, Any]) -> str | None:
    """Persist one trajectory record, returning its filename or ``None``.

    Trajectory capture is audit evidence rather than a measured outcome, so a
    write failure must never abort a paid cell: the caller simply records that
    no file was produced and the trial keeps its aggregate counts.
    """

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError, RecursionError):
        return None
    return path.name


def _persist_phase_actions(phase: Any, phase_dir: Path) -> str | None:
    """Write one Agent-S phase's decided actions beside its step frames.

    ``_phase_trace`` keeps only counts because its dict feeds the Start snapshot
    fingerprint, so the decided sequence would otherwise be discarded once the
    phase returns.
    """

    actions = getattr(phase, "actions", ())
    if not isinstance(actions, (list, tuple)):
        return None
    return _write_trajectory_artifact(
        Path(phase_dir) / PHASE_ACTIONS_FILENAME,
        {
            "schema": "c1-lazarus-phase-actions-v1",
            "steps": _phase_steps(phase),
            "action_count": len(actions),
            "incomplete": bool(getattr(phase, "incomplete", False)),
            "actions": [
                dict(action) if isinstance(action, Mapping) else action
                for action in actions
            ],
        },
    )


def _persist_cost_ledger(artifact_dir: Path, usage: Mapping[str, Any]) -> str | None:
    """Write the provider call ledger that ``CostMeter.summary`` already carries.

    The summary embeds one entry per provider call, including the sanitized
    request messages and the assistant reply.  Keeping it on disk instead of
    inside the runtime trace leaves the self-reported-verdict guard scanning
    only trusted keys, and keeps the run report small.
    """

    return _write_trajectory_artifact(
        Path(artifact_dir) / COST_LEDGER_FILENAME,
        {"schema": "c1-lazarus-cost-ledger-v1", "usage": dict(usage)},
    )


def _record_agent_s_trajectory(
    artifact_dir: Path | None, usage: Mapping[str, Any]
) -> dict[str, Any]:
    """Write the policy call ledger and describe where the trajectory landed."""

    if artifact_dir is None:
        return {
            "schema": "c1-lazarus-agent-s-trajectory-v1",
            "recorded": False,
            "reason": "runtime_did_not_report_an_artifact_dir",
        }
    root = Path(artifact_dir)
    trace: dict[str, Any] = {
        "schema": "c1-lazarus-agent-s-trajectory-v1",
        "recorded": True,
        "artifact_dir": str(root),
        "phase_actions": [
            f"{name}/{PHASE_ACTIONS_FILENAME}"
            for name in ("phase-a", "phase-b")
            if (root / name / PHASE_ACTIONS_FILENAME).is_file()
        ],
    }
    ledger = _persist_cost_ledger(root, usage)
    if ledger is not None:
        trace["cost_ledger"] = ledger
    return trace


def _incomplete_agent_s_execution(
    *,
    started: float,
    output: Path,
    before_path: Path | None,
    source: Path,
    sync: Mapping[str, Any],
    phase_a: Mapping[str, Any],
    phase_b: Mapping[str, Any] | None,
    after_path: Path | None = None,
) -> LazarusRuntimeExecution:
    trace: dict[str, Any] = {
        "schema": "c1-lazarus-luanti-agent-s-execution-v1",
        "source_clone": str(source),
        "asset_sync": dict(sync),
        "phase_a": dict(phase_a),
        "runtime_status": "incomplete",
    }
    if phase_b is not None:
        trace["phase_b"] = dict(phase_b)
    return LazarusRuntimeExecution(
        complete=False,
        runtime_restored=True,
        before_semantic_ok=None,
        goal_checks_ok=None,
        defect_signature_ok=None,
        invariants_ok=None,
        before_screenshot=before_path,
        after_screenshot=after_path,
        runtime_time_ms=int((time.perf_counter() - started) * 1000),
        trace=trace,
        artifact_dir=output,
    )


def _accept_state_generated_spec(
    candidate: Any, *, issue: PublicIssue, seed: int
) -> dict[str, Any] | None:
    acceptance = accept_c1_generated_spec(
        candidate,
        issue=issue,
        benchmark=issue.benchmark,
        seed=seed,
    )
    if not acceptance.accepted:
        raise ValueError(
            "Lazarus state generated checkpoint was rejected: "
            + str(acceptance.reason or "unknown reason")
        )
    return acceptance.spec


def _usage_count(usage: Any, field: str) -> int:
    if not isinstance(usage, Mapping):
        raise ValueError("Lazarus synthesis usage accounting is missing")
    value = usage.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Lazarus synthesis usage {field} is invalid")
    if not math.isfinite(float(value)) or float(value) < 0:
        raise ValueError(f"Lazarus synthesis usage {field} is invalid")
    return int(value)


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


@contextmanager
def _without_environment(name: str) -> Iterator[None]:
    """Temporarily remove inherited coordinator state from an Agent-S process."""

    previous = os.environ.pop(name, None)
    try:
        yield
    finally:
        if previous is not None:
            os.environ[name] = previous


def _public_issue(context: LazarusPhase0CellContext) -> PublicIssue:
    """Reconstruct the model payload from public fields only."""

    task = context.task
    required = ("id", "benchmark", "repo", "state_description")
    values = {name: str(task.get(name) or "").strip() for name in required}
    if any(not value for value in values.values()):
        raise ValueError("Lazarus state task is missing a public field")
    tags = task.get("tags")
    if not isinstance(tags, list) or not all(isinstance(item, str) for item in tags):
        tags = []
    random_control = task.get("random_control")
    if not isinstance(random_control, Mapping):
        random_control = {}
    budget_ms = task.get("budget_ms")
    if isinstance(budget_ms, bool) or not isinstance(budget_ms, int) or budget_ms <= 0:
        budget_ms = 2_400_000
    return PublicIssue(
        id=values["id"],
        benchmark=values["benchmark"],
        repo=values["repo"],
        issue=values["state_description"],
        tags=list(tags),
        budget_ms=budget_ms,
        prompt_revision=str(task.get("prompt_revision") or "").strip(),
        random_control=dict(random_control),
        source_path=None,
    )


def _validate_synthesis(value: LazarusSynthesizedCheckpoint) -> None:
    if not isinstance(value, LazarusSynthesizedCheckpoint):
        raise TypeError("state synthesis did not return LazarusSynthesizedCheckpoint")
    if not isinstance(value.generated_spec, Mapping) or not value.generated_spec:
        raise ValueError("state synthesis generated_spec is missing")
    if value.actual_synthesis_calls < 1:
        raise ValueError("buggy state synthesis must record an actual provider call")
    _validate_measurement(value.treatment_tokens, "state synthesis treatment_tokens")
    _validate_measurement(value.treatment_time_ms, "state synthesis treatment_time_ms")
    if not isinstance(value.trace, Mapping):
        raise ValueError("state synthesis trace is missing")
    _reject_final_verdict(value.trace, "state synthesis trace")


def _validate_execution(value: LazarusRuntimeExecution) -> None:
    if not isinstance(value, LazarusRuntimeExecution):
        raise TypeError("state runtime did not return LazarusRuntimeExecution")
    _validate_measurement(value.runtime_time_ms, "state execution runtime_time_ms")
    if not isinstance(value.trace, Mapping):
        raise ValueError("state execution trace is missing")
    _reject_final_verdict(value.trace, "state execution trace")
    if value.complete is not True:
        return
    if value.runtime_restored is not True:
        return
    if type(value.before_semantic_ok) is not bool:
        raise ValueError("state execution before_semantic_ok is missing")
    before = value.before_screenshot
    if not isinstance(before, Path) or not before.is_file():
        raise ValueError("state execution before_screenshot is missing")
    if value.before_semantic_ok is not True:
        return
    for name in ("goal_checks_ok", "defect_signature_ok", "invariants_ok"):
        if type(getattr(value, name)) is not bool:
            raise ValueError(f"state execution {name} is missing")
    after = value.after_screenshot
    if not isinstance(after, Path) or not after.is_file():
        raise ValueError("state execution after_screenshot is missing")


def _validate_measurement(value: int | float, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} is invalid")


def _checkpoint_sha256(spec: Mapping[str, Any]) -> str:
    try:
        encoded = json.dumps(
            spec,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("state generated_spec is not canonical JSON") from error
    return hashlib.sha256(encoded).hexdigest()


def _attach_lazarus_usage(error: Exception, usage: Mapping[str, Any]) -> None:
    if not isinstance(getattr(error, "lazarus_usage", None), Mapping):
        setattr(error, "lazarus_usage", dict(usage))


def _synthesis_usage(
    synthesis: LazarusSynthesizedCheckpoint, *, cache_hit: bool
) -> dict[str, Any]:
    return {
        "llm_calls": 0 if cache_hit else int(synthesis.actual_synthesis_calls),
        "total_tokens": 0 if cache_hit else int(synthesis.treatment_tokens),
        "treatment_time_ms": (
            0 if cache_hit else int(synthesis.treatment_time_ms)
        ),
        "usage_complete": True,
    }


def _reject_final_verdict(value: Any, label: str) -> None:
    if _contains_final_verdict(value):
        raise ValueError(f"{label} contains a self-reported final verdict")


def _contains_final_verdict(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            str(key) in _FINAL_VERDICT_FIELDS or _contains_final_verdict(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_contains_final_verdict(item) for item in value)
    return False


__all__ = [
    "LazarusAgentSExecution",
    "LazarusAgentSRuntimeRunner",
    "LazarusLLMStateSynthesis",
    "LazarusPhase0RuntimeUnsupportedError",
    "LazarusRuntimeExecution",
    "LazarusStateExecution",
    "LazarusStateRuntimeRunner",
    "LazarusStateSynthesis",
    "LazarusSynthesizedCheckpoint",
    "LuantiLazarusAgentSExecution",
    "LuantiLazarusStateExecution",
    "OpenRAHotkeyLazarusAgentSExecution",
    "OpenRAHotkeyLazarusStateExecution",
    "build_lazarus_formal_runtime_runner",
    "build_lazarus_phase0_runtime_runner",
    "phase0_runtime_support_report",
    "validate_lazarus_phase0_runtime_support",
]
