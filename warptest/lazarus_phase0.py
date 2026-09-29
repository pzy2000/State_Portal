"""Fail-closed preparation and serial dispatch for the Lazarus Phase-0 pilot.

The two-task pilot is intentionally separate from the future 24-task matrix.
This module owns only its coordinator-side state: it never puts a defect block,
an oracle, a patch, or a version label in the synthesis source tree passed to a
method.  A caller must provide an execution callback after this preflight has
completed; that separation prevents a dry-run or an incomplete calibration from
silently reaching a provider.
"""

from __future__ import annotations

import hashlib
import contextlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .c1_sandbox import preflight_c1_sandboxes, strict_clone_tree
from .c1_provider_runtime import C1_PROVIDER_TIMEOUT_S, provider_timeout_environment
from .c1_experiment import relay_environment
from .lazarus_aggregate import lazarus_from_reports
from .lazarus_cell_contract import (
    nearest_rank_p95,
    validate_lazarus_cell_report,
)
from .lazarus_cell_contract import observed_p2p_p95_s as _shared_observed_p2p_p95_s
from .lazarus_calibration import (
    lazarus_backend_binding_from_artifact,
    lazarus_harness_hash,
    lazarus_provider_identity_sha256,
    validate_lazarus_calibration,
)
from .lazarus_catalog import LAZARUS_VERSIONS, validate_lazarus_catalog
from .lazarus_grading import (
    LazarusTrialEvidence,
    VisualJudgement,
    grade_lazarus_trial,
)
from .lazarus_goldens import validate_lazarus_golden_set
from .lazarus_manifest import (
    LazarusPhase0ManifestContract,
    validate_lazarus_phase0_manifest,
)
from .lazarus_mining import validate_lazarus_trial_provenance
from .lazarus_openra_hotkey_probe import (
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    create_openra_pr21453_prebuild_callback,
)
from .lazarus_orchestrator import (
    PHASE0_DECISION_MODEL,
    LazarusStateReuseTracker,
    LazarusTrialKey,
    LazarusTrialPlan,
    build_lazarus_trial_plan,
)
from .model_request import model_request_profiles_environment
from .repo_tools import RepoSandbox
from .lazarus_trial import (
    LazarusRuntimeObservation,
    LazarusVisualEvidence,
    judge_lazarus_visual_evidence,
    lazarus_p2p_checkpoint_restore_is_valid,
    run_lazarus_trial,
)
from .lazarus_upstream import (
    UpstreamTestDefinition,
    definitions_from_upstream_record,
    run_lazarus_per_trial_p2p,
)
from .lazarus_versions import (
    LazarusVersionArtifact,
    LazarusVersionPair,
    LazarusVersionRequest,
    materialize_lazarus_versions,
    tree_fingerprint,
)


PHASE0_MANIFEST = Path("benchmarks/p5/lazarus_phase0_manifest.yaml")
PHASE0_CATALOG = Path("benchmarks/p5/c1_lazarus_phase0_tasks.yaml")
PHASE0_CALIBRATION = Path("reports/lazarus/phase0-calibration.json")
PHASE0_AGENT_VIEW_ROOT = Path(".warptest-cache/lazarus/agent-views/phase0")
PHASE0_AGENT_GIT_ROOT = Path(".warptest-cache/lazarus/agent-git/phase0")
PHASE0_P2P_TIMEOUT_S = 300.0
PHASE0_JUDGE_MODEL = "qwen3-vl-flash@relay"
PHASE0_OBSERVER_MODEL = "claude-sonnet-5@relay"
_UNITY_EDITOR_VERSION_RE = re.compile(r"\b\d{4}\.\d+\.\d+[a-z]\d+(?:c\d+)?\b")
_PHASE0_UNITY_PROJECTS = {
    "jynew": Path("games/jynew/jyx2"),
    "pal3": Path("games/Pal3.Unity"),
}


@dataclass(frozen=True)
class AgentVisibleSource:
    """A source-only tree exposed to synthesis, never to the runtime grader."""

    task_id: str
    root: Path
    trusted_git_dir: Path
    source_revision: str
    source_tree_sha256: str
    sanitized_tree_sha256: str
    forbidden_tokens: tuple[str, ...]
    version_identity_hidden: bool


@dataclass(frozen=True)
class LazarusPhase0Preflight:
    """All evidence that must exist before an eight-cell provider dispatch."""

    contract: LazarusPhase0ManifestContract | None
    tasks: tuple[dict[str, Any], ...]
    plan: LazarusTrialPlan
    pairs: dict[str, LazarusVersionPair]
    agent_sources: dict[str, AgentVisibleSource]
    expected_provenance: dict[tuple[str, str], str]
    per_trial_p2p_ids: dict[str, tuple[str, ...]]
    p2p_p95_s: float | None
    environment: dict[str, Any]
    manifest_sha256: str
    harness_sha256: str
    provider_identity_sha256: str
    task_evidence: dict[str, dict[str, str]]
    complete: bool
    failures: tuple[str, ...]

    def to_report(self) -> dict[str, Any]:
        """Return the durable pre-dispatch evidence without task-private data.

        The coordinator keeps complete task records only in memory.  The
        persisted preflight artifact deliberately contains the registered cell
        identities and provenance bindings, but no issue wording, defect block,
        hidden oracle, patch path, or upstream conversation payload.
        """

        return {
            "schema": "c1-lazarus-phase0-preflight-v1",
            "schema_version": 1,
            "complete": self.complete,
            "failures": list(self.failures),
            "expected_cells": self.plan.trial_count,
            "plan": {
                "formal": self.plan.formal,
                "rotation_rule": self.plan.rotation_rule,
                "cells": [
                    {
                        "task_id": key.task_id,
                        "arm": key.arm,
                        "model": key.model,
                        "seed": key.seed,
                        "version": key.version,
                    }
                    for key in self.plan.keys
                ],
            },
            "p2p_p95_s": self.p2p_p95_s,
            "environment": dict(self.environment),
            "identity": {
                "manifest_sha256": self.manifest_sha256,
                "harness_sha256": self.harness_sha256,
                "provider_identity_sha256": self.provider_identity_sha256,
            },
            "task_evidence": {
                task_id: dict(values)
                for task_id, values in sorted(self.task_evidence.items())
            },
            "expected_provenance": {
                f"{task_id}/{version}": sha256
                for (task_id, version), sha256 in sorted(
                    self.expected_provenance.items()
                )
            },
            "agent_views": {
                task_id: {
                    "source_revision": source.source_revision,
                    "source_tree_sha256": source.source_tree_sha256,
                    "sanitized_tree_sha256": source.sanitized_tree_sha256,
                    "version_identity_hidden": source.version_identity_hidden,
                }
                for task_id, source in sorted(self.agent_sources.items())
            },
        }


@dataclass(frozen=True)
class LazarusPhase0CellContext:
    """Coordinator-only data passed to one trusted arm executor."""

    key: LazarusTrialKey
    task: Mapping[str, Any]
    artifact: LazarusVersionArtifact
    agent_source: AgentVisibleSource
    defect_locus: str
    per_trial_p2p_ids: tuple[str, ...]
    version_switch_ms: float = 0.0


@dataclass(frozen=True)
class LazarusPhase0Run:
    """Serialized result of a serial Phase-0 attempt, including early stops."""

    complete: bool
    stopped_early: bool
    stop_reason: str | None
    reports: tuple[dict[str, Any], ...]
    aggregate: dict[str, Any] | None
    observed_p2p_p95_s: float | None = None

    def to_report(self, preflight: LazarusPhase0Preflight) -> dict[str, Any]:
        """Return a JSON-safe coordinator record without exposing task text."""

        return {
            "schema": "c1-lazarus-phase0-run-v1",
            "schema_version": 1,
            "complete": self.complete,
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
            "expected_cells": preflight.plan.trial_count,
            "observed_cells": len(self.reports),
            "observed_p2p_p95_s": self.observed_p2p_p95_s,
            "preflight": {
                "complete": preflight.complete,
                "failures": list(preflight.failures),
                "p2p_p95_s": preflight.p2p_p95_s,
                "identity": {
                    "manifest_sha256": preflight.manifest_sha256,
                    "harness_sha256": preflight.harness_sha256,
                    "provider_identity_sha256": preflight.provider_identity_sha256,
                },
                "task_evidence": {
                    task_id: dict(values)
                    for task_id, values in sorted(preflight.task_evidence.items())
                },
                "expected_provenance": {
                    f"{task_id}/{version}": sha256
                    for (task_id, version), sha256 in sorted(
                        preflight.expected_provenance.items()
                    )
                },
                "agent_views": {
                    task_id: {
                        "source_revision": source.source_revision,
                        "source_tree_sha256": source.source_tree_sha256,
                        "sanitized_tree_sha256": source.sanitized_tree_sha256,
                        "version_identity_hidden": source.version_identity_hidden,
                    }
                    for task_id, source in sorted(preflight.agent_sources.items())
                },
            },
            "reports": [dict(report) for report in self.reports],
            "aggregate": self.aggregate,
        }


@dataclass(frozen=True)
class LazarusPhase0Launch:
    """One dedicated Phase-0 launch and its durable coordinator artifact."""

    preflight: LazarusPhase0Preflight
    run: LazarusPhase0Run
    report_path: Path


Phase0Executor = Callable[[LazarusPhase0CellContext], Mapping[str, Any]]
Phase0RuntimeRunner = Callable[[LazarusPhase0CellContext], LazarusRuntimeObservation]
Phase0VisualRunner = Callable[
    [
        LazarusPhase0CellContext,
        LazarusRuntimeObservation,
        Mapping[str, Any],
        Path,
        bool,
    ],
    LazarusVisualEvidence,
]
Phase0P2PRunner = Callable[
    [LazarusPhase0CellContext, Mapping[str, UpstreamTestDefinition]], Mapping[str, Any]
]


def build_lazarus_phase0_executor(
    root: str | Path,
    *,
    runtime_runner: Phase0RuntimeRunner,
    visual_runner: Phase0VisualRunner | None = None,
    p2p_runner: Phase0P2PRunner | None = None,
    p2p_sandbox_root: str | Path = ".warptest-cache/lazarus/p2p/phase0",
    judge_model: str = PHASE0_JUDGE_MODEL,
    grading_mode: str = "semantic_visual",
) -> Phase0Executor:
    """Bind Phase-0 task artifacts to the raw-evidence trial coordinator.

    The caller supplies a concrete arm/runtime adapter.  This function supplies
    the only permitted path to grading: it reloads the frozen private oracle and
    upstream record, invokes the dual visual evaluator, and launches a fresh
    registered P2P subset after the runtime reports a restored state.  The arm
    cannot choose its own tests, goldens, or final verdict fields.
    """

    workspace = Path(root).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Lazarus Phase-0 executor workspace does not exist")
    if judge_model != PHASE0_JUDGE_MODEL:
        raise ValueError("Lazarus Phase-0 judge model does not match the manifest")
    if grading_mode not in {"semantic_visual", "p2p_only_visual"}:
        raise ValueError("unknown Lazarus executor grading mode")
    sandbox = _contained(workspace, p2p_sandbox_root, "P2P sandbox root")

    def execute(context: LazarusPhase0CellContext) -> Mapping[str, Any]:
        task = context.task
        defect = task.get("defect") if isinstance(task, Mapping) else None
        if not isinstance(defect, Mapping):
            raise ValueError(f"{context.key.task_id}: private defect metadata is missing")
        spec_path = _contained(
            workspace,
            str(task.get("oracle_spec") or ""),
            f"{context.key.task_id} oracle",
        )
        spec = _load_yaml_mapping(spec_path, f"{context.key.task_id} oracle")
        upstream_value = defect.get("upstream_record")
        if not isinstance(upstream_value, str) or not upstream_value.strip():
            raise ValueError(f"{context.key.task_id}: upstream record is missing")
        upstream_path = _contained(
            workspace,
            upstream_value,
            f"{context.key.task_id} upstream record",
        )
        upstream = _load_json_mapping(upstream_path, f"{context.key.task_id} upstream record")
        clean_source_root = _contained(
            workspace,
            str(task.get("repo") or ""),
            f"{context.key.task_id} clean source repository",
        )
        upstream_errors = validate_lazarus_trial_provenance(
            upstream,
            source_root=clean_source_root,
            harness_root=workspace,
        )
        if upstream_errors:
            raise ValueError(
                f"{context.key.task_id}: upstream record is invalid: "
                + "; ".join(upstream_errors)
            )
        definitions = definitions_from_upstream_record(upstream)
        observation: LazarusRuntimeObservation | None = None

        def run_runtime() -> LazarusRuntimeObservation:
            nonlocal observation
            observation = runtime_runner(context)
            return observation

        def run_visual(
            observation: LazarusRuntimeObservation, evaluate_after: bool
        ) -> LazarusVisualEvidence:
            if visual_runner is not None:
                return visual_runner(
                    context, observation, spec, spec_path, evaluate_after
                )
            return judge_lazarus_visual_evidence(
                observation,
                spec=spec,
                spec_path=spec_path,
                evaluate_after=evaluate_after,
                judge_model=judge_model,
            )

        def run_p2p() -> Mapping[str, Any]:
            if p2p_runner is not None:
                return p2p_runner(context, definitions)
            if observation is None:
                raise RuntimeError("P2P cannot run before the Lazarus runtime observation")
            if observation.p2p_restore_adapter is None:
                raise RuntimeError(
                    "P2P cannot run without a runtime-bound isolated state restorer"
                )
            return run_lazarus_per_trial_p2p(
                task_id=context.key.task_id,
                version=context.key.version,
                checkpoint_sha256=observation.checkpoint_sha256,
                checkpoint_kind=(
                    "semantic_state"
                    if context.key.arm == "lazarus_state"
                    else "agent_s_from_start"
                ),
                source_root=context.artifact.root,
                definitions=definitions,
                test_ids=context.per_trial_p2p_ids,
                sandbox_root=sandbox / context.key.invocation_id(),
                harness_root=workspace,
                trusted_git_dir=context.artifact.git_metadata,
                checkpoint_restorer=observation.p2p_restore_adapter,
            )

        return run_lazarus_trial(
            key=context.key,
            task_id=context.key.task_id,
            defect_locus=context.defect_locus,
            artifact=context.artifact,
            per_trial_p2p_ids=context.per_trial_p2p_ids,
            version_switch_ms=context.version_switch_ms,
            runtime_runner=run_runtime,
            visual_runner=run_visual,
            p2p_runner=run_p2p,
            grading_mode=grading_mode,
        )

    return execute


_BACKENDS: dict[str, dict[str, Any]] = {
    "luanti": {
        "capture_backend": "luanti_irrlicht_create_screenshot_v1",
        "input_backend": "luanti_irrlicht_sevent_v1",
        "binary_paths": ("build-app/out/luanti.app/Contents/MacOS/luanti",),
    },
    "openra": {
        "capture_backend": "openra_renderer_save_screenshot_sync_v1",
        "input_backend": "openra_default_input_handler_v1",
        "binary_paths": (
            "bin/OpenRA.Game.dll",
            "bin/OpenRA.Mods.Common.dll",
            "bin/OpenRA.Test.dll",
        ),
    },
}

_PRIVATE_SOURCE_TOKENS: dict[str, tuple[str, ...]] = {
    "lz-luanti-11919-vector-from-string": (),
    # The pinned Luanti source carries no task fixture, so only the frozen
    # selection identifiers must be absent from the agent-visible view.
    "lz-luanti-14757-math-round": (
        "c1_lazarus_round",
        "C1_LAZARUS_14757",
        "lz-luanti-14757-math-round",
    ),
    "lz-openra-21453-hotkey-ready-building": (
        "WARPTEST_OPENRA_HOTKEY_AUDIT_PATH",
        "WarptestReadyBuildingAudit",
        "WARPREADY",
        "lz-openra-21453-hotkey-ready-building",
    ),
}


def lazarus_private_source_tokens(task_id: str) -> tuple[str, ...]:
    """Return the private fixture tokens an agent-visible view must not contain.

    A row with no registered tokens has no task fixture in its pinned source, so
    only the version identity itself has to be hidden.  A row that ships a
    fixture must register it here before it can be dispatched, which is why the
    formal orchestrator reads the same map instead of keeping its own.
    """

    return tuple(_PRIVATE_SOURCE_TOKENS.get(str(task_id or ""), ()))


def prepare_lazarus_agent_source(
    *,
    task_id: str,
    source_repo: str | Path,
    source_revision: str,
    destination: str | Path,
    trusted_git_dir: str | Path,
    forbidden_tokens: Sequence[str],
) -> AgentVisibleSource:
    """Create a source-only CoW view with Git and private fixture clues removed.

    The view comes from the clean source at the row's registered revision, not
    from a task's prebuilt runtime overlay.  It is therefore byte-identical for
    the fixed and buggy cells.  Its Git metadata is moved beside, rather than
    inside, the agent root so the synthesis tool surface cannot inspect patch
    history.

    A row whose defect predates the pinned submodule revision registers its own
    base, so the disposable clone is moved to that revision.  The shared checkout
    is only required to be clean and to contain it.
    """

    source = Path(source_repo).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    trusted = Path(trusted_git_dir).expanduser().resolve()
    if not source.is_dir():
        raise ValueError("Lazarus agent source repository does not exist")
    if target.exists() or trusted.exists():
        raise ValueError("Lazarus agent source destination already exists")
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("Lazarus agent source destination overlaps source repository")
    _require_clean_source(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    trusted.parent.mkdir(parents=True, exist_ok=True)
    strict_clone_tree(source, target)
    _stage_clone_revision(target, source_revision)
    _remove_git_ignored_paths(target)
    git_path = target / ".git"
    if not git_path.exists():
        raise ValueError("Lazarus agent source clone is missing Git metadata")
    os.replace(git_path, trusted)
    if (target / ".git").exists() or not trusted.exists():
        raise ValueError("Lazarus agent source did not isolate Git metadata")
    normalized_tokens = tuple(
        sorted({str(value) for value in forbidden_tokens if str(value)})
    )
    _assert_agent_source_safe(target, normalized_tokens)
    if len(RepoSandbox(target).build_repo_map().splitlines()) <= 1:
        raise ValueError("Lazarus agent source exploration surface is empty")
    sanitized = tree_fingerprint(target)
    return AgentVisibleSource(
        task_id=str(task_id),
        root=target,
        trusted_git_dir=trusted,
        source_revision=str(source_revision),
        source_tree_sha256=sanitized,
        sanitized_tree_sha256=sanitized,
        forbidden_tokens=normalized_tokens,
        version_identity_hidden=True,
    )


def build_lazarus_phase0_preflight(
    *,
    tasks: Sequence[Mapping[str, Any]],
    pairs: Mapping[str, LazarusVersionPair],
    calibration: Mapping[str, Any],
    manifest_sha256: str,
    harness_sha256: str,
    provider_identity_sha256: str,
    backend_paths: Mapping[str, Sequence[str]],
    capture_backends: Mapping[str, str],
    input_backends: Mapping[str, str],
    gate_reports: Mapping[str, Mapping[str, Any]],
    per_trial_p2p_ids: Mapping[str, Sequence[str]],
    agent_sources: Mapping[str, AgentVisibleSource],
    task_evidence: Mapping[str, Mapping[str, Any]],
    environment: Mapping[str, Any],
    contract: LazarusPhase0ManifestContract | None = None,
) -> LazarusPhase0Preflight:
    """Validate all non-provider Phase-0 inputs as one fail-closed public seam."""

    failures: list[str] = []
    normalized_tasks = tuple(dict(task) for task in tasks)
    normalized_p2p_ids = {
        str(task_id): tuple(str(value) for value in values)
        for task_id, values in per_trial_p2p_ids.items()
    }
    normalized_task_evidence = {
        str(task_id): {
            str(name): str(value)
            for name, value in record.items()
        }
        for task_id, record in task_evidence.items()
        if isinstance(record, Mapping)
    }
    task_by_id: dict[str, dict[str, Any]] = {}
    for task in normalized_tasks:
        task_id = str(task.get("id") or "")
        if not task_id or task_id in task_by_id:
            failures.append("Phase 0 task ids must be non-empty and unique")
            continue
        task_by_id[task_id] = task
    task_ids = tuple(task_by_id)
    plan = build_lazarus_trial_plan(
        task_ids,
        seeds=(0,),
        model=PHASE0_DECISION_MODEL,
        formal=False,
    )
    if not plan.complete:
        failures.extend(plan.failures)
    if len(task_ids) != 2:
        failures.append("Phase 0 requires exactly two tasks")
    _validate_phase0_task_shapes(task_by_id, failures)
    if contract is not None and task_ids != contract.task_ids:
        failures.append("Phase 0 task order does not match its manifest contract")

    expected_versions: dict[tuple[str, str], dict[str, Any]] = {}
    expected_provenance: dict[tuple[str, str], str] = {}
    p2p_durations_s: list[float] = []
    for task_id, task in task_by_id.items():
        benchmark = str(task.get("benchmark") or "")
        pair = pairs.get(task_id)
        if pair is None:
            failures.append(f"{task_id}: materialized version pair is missing")
            continue
        if benchmark not in backend_paths:
            failures.append(f"{task_id}: no Phase-0 backend paths for {benchmark}")
            continue
        if not capture_backends.get(benchmark) or not input_backends.get(benchmark):
            failures.append(f"{task_id}: backend identity is incomplete")
            continue
        for version, artifact in (("buggy", pair.buggy), ("fixed", pair.fixed)):
            if artifact.task_id != task_id or artifact.version != version:
                failures.append(f"{task_id}/{version}: materialized artifact identity drift")
                continue
            try:
                binding = lazarus_backend_binding_from_artifact(
                    artifact,
                    benchmark=benchmark,
                    capture_backend=str(capture_backends[benchmark]),
                    input_backend=str(input_backends[benchmark]),
                    binary_paths=tuple(backend_paths[benchmark]),
                    canary_passed=True,
                )
            except ValueError as error:
                failures.append(f"{task_id}/{version}: calibration binding failed: {error}")
                continue
            expected_versions[(task_id, version)] = {
                "benchmark": binding.benchmark,
                "source_tree_sha256": binding.source_tree_sha256,
                "prebuilt_binary_sha256": binding.prebuilt_binary_sha256,
                "capture_backend": binding.capture_backend,
                "input_backend": binding.input_backend,
            }
            expected_provenance[(task_id, version)] = artifact.tree_sha256
        view = agent_sources.get(task_id)
        if view is None:
            failures.append(f"{task_id}: agent-visible source is missing")
        elif view.task_id != task_id or not view.version_identity_hidden:
            failures.append(f"{task_id}: agent-visible source does not hide version identity")
        evidence_record = normalized_task_evidence.get(task_id)
        golden_metadata_sha256 = _validate_task_evidence(
            task_id,
            evidence_record,
            failures,
        )
        report = gate_reports.get(task_id)
        if report is None:
            failures.append(f"{task_id}: construction gate report is missing")
        else:
            _validate_gate_report(
                task_id,
                pair,
                report,
                failures,
                golden_metadata_sha256=golden_metadata_sha256,
            )
            p2p_durations_s.extend(
                _gate_p2p_durations(
                    task_id,
                    report,
                    normalized_p2p_ids.get(task_id, ()),
                    failures,
                )
            )

    calibration_errors = validate_lazarus_calibration(
        calibration,
        expected_versions=expected_versions,
        manifest_sha256=manifest_sha256,
        harness_sha256=harness_sha256,
        provider_identity_sha256=provider_identity_sha256,
    )
    failures.extend(calibration_errors)
    for name in ("apfs", "submodules_clean", "capacity_ok", "unity_editors"):
        if environment.get(name) is not True:
            failures.append(f"Phase 0 environment check failed: {name}")
    if environment.get("visual_judge_fake") is True:
        failures.append("Phase 0 cannot run with a fake visual judge")
    p95 = nearest_rank_p95(p2p_durations_s)
    if p95 is None:
        failures.append("Phase 0 per-trial P2P duration evidence is missing")
    elif p95 > PHASE0_P2P_TIMEOUT_S:
        failures.append(
            "Phase 0 per-trial P2P P95 exceeds 300 seconds; shrink the subset or stop"
        )
    return LazarusPhase0Preflight(
        contract=contract,
        tasks=normalized_tasks,
        plan=plan,
        pairs=dict(pairs),
        agent_sources=dict(agent_sources),
        expected_provenance=expected_provenance,
        per_trial_p2p_ids=normalized_p2p_ids,
        p2p_p95_s=p95,
        environment=dict(environment),
        manifest_sha256=manifest_sha256,
        harness_sha256=harness_sha256,
        provider_identity_sha256=provider_identity_sha256,
        task_evidence=normalized_task_evidence,
        complete=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


def preflight_lazarus_phase0(
    root: str | Path = Path.cwd(),
    *,
    manifest_path: str | Path = PHASE0_MANIFEST,
    catalog_path: str | Path = PHASE0_CATALOG,
    calibration_path: str | Path = PHASE0_CALIBRATION,
    cache_root: str | Path = ".warptest-cache/lazarus",
    agent_view_root: str | Path = PHASE0_AGENT_VIEW_ROOT,
    agent_git_root: str | Path = PHASE0_AGENT_GIT_ROOT,
) -> LazarusPhase0Preflight:
    """Materialize and verify the real Phase-0 pilot without provider access.

    A malformed provenance population returns an incomplete preflight record
    before any materialization so the canonical preflight CLI can still persist
    its no-dispatch evidence.
    """

    workspace = Path(root).expanduser().resolve()
    manifest_file = _contained(workspace, manifest_path, "manifest")
    catalog_file = _contained(workspace, catalog_path, "catalog")
    calibration_file = _contained(workspace, calibration_path, "calibration")
    cache = _contained(workspace, cache_root, "cache root")
    view_root = _contained(workspace, agent_view_root, "agent view root")
    git_root = _contained(workspace, agent_git_root, "agent Git root")
    manifest = _load_yaml_mapping(manifest_file, "Lazarus Phase 0 manifest")
    catalog = _load_yaml_mapping(catalog_file, "Lazarus Phase 0 catalog")
    hosts, tasks = validate_lazarus_catalog(catalog, source_path=catalog_file)
    contract = validate_lazarus_phase0_manifest(
        manifest,
        catalog_task_ids=[str(task["id"]) for task in tasks],
    )
    # Reject unsupported provenance before materializing a pair or building an
    # overlay.  Manual issue/PR instructions remain useful mining evidence but
    # cannot enter the paid-pilot candidate population as upstream F2P/P2P.
    provenance_failures: list[str] = []
    provenance_evidence: dict[str, dict[str, str]] = {}
    for task in tasks:
        task_id = str(task["id"])
        defect = task.get("defect")
        if not isinstance(defect, Mapping):
            provenance_failures.append(f"{task_id}: private defect metadata is missing")
            continue
        upstream_value = defect.get("upstream_record")
        if not isinstance(upstream_value, str) or not upstream_value.strip():
            provenance_failures.append(f"{task_id}: upstream record is missing")
            continue
        try:
            upstream_path = _contained(
                workspace, upstream_value, f"{task_id} upstream record"
            )
            upstream = _load_json_mapping(upstream_path, f"{task_id} upstream record")
        except ValueError as error:
            provenance_failures.append(f"{task_id}: {error}")
            continue
        provenance_evidence[task_id] = {
            "upstream_evidence_sha256": _sha256(upstream_path),
        }
        try:
            source_root = _contained(
                workspace,
                str(task.get("repo") or ""),
                f"{task_id} source repository",
            )
        except ValueError as error:
            provenance_failures.append(f"{task_id}: {error}")
            continue
        for error in validate_lazarus_trial_provenance(
            upstream, source_root=source_root, harness_root=workspace
        ):
            provenance_failures.append(f"{task_id}: upstream provenance invalid: {error}")
    if provenance_failures:
        return _incomplete_provenance_preflight(
            tasks=tasks,
            contract=contract,
            manifest_sha256=_sha256(manifest_file),
            harness_sha256=lazarus_harness_hash(workspace),
            provider_identity_sha256=lazarus_provider_identity_sha256(manifest),
            task_evidence=provenance_evidence,
            failures=provenance_failures,
        )
    calibration = _load_json_mapping(calibration_file, "Lazarus Phase 0 calibration")
    pairs: dict[str, LazarusVersionPair] = {}
    views: dict[str, AgentVisibleSource] = {}
    gate_reports: dict[str, Mapping[str, Any]] = {}
    per_trial_ids: dict[str, tuple[str, ...]] = {}
    task_evidence: dict[str, dict[str, str]] = {}
    for task in tasks:
        task_id = str(task["id"])
        request = _phase0_version_request(workspace, task_id)
        callback = (
            create_openra_pr21453_prebuild_callback()
            if task_id == OPENRA_HOTKEY_PHASE0_TASK_ID
            else None
        )
        pairs[task_id] = materialize_lazarus_versions(
            request,
            cache_root=cache,
            build=callback,
        )
        source = Path(request.source_repo).resolve()
        view_dir = view_root / task_id / f"{request.fixed_revision[:12]}-{time.time_ns()}"
        trusted_git = git_root / task_id / f"{request.fixed_revision[:12]}-{time.time_ns()}.git"
        views[task_id] = prepare_lazarus_agent_source(
            task_id=task_id,
            source_repo=source,
            source_revision=request.fixed_revision,
            destination=view_dir,
            trusted_git_dir=trusted_git,
            forbidden_tokens=_PRIVATE_SOURCE_TOKENS.get(task_id, ()),
        )
        defect = task.get("defect")
        if not isinstance(defect, Mapping):
            raise ValueError(f"{task_id}: private defect metadata is missing")
        upstream_path = _contained(
            workspace,
            str(defect.get("upstream_record") or ""),
            f"{task_id} upstream record",
        )
        spec_path = _contained(
            workspace,
            str(task["oracle_spec"]),
            f"{task_id} oracle",
        )
        gate_reports[task_id] = _load_json_mapping(
            _contained(workspace, str(defect["gate_report"]), f"{task_id} gate"),
            f"{task_id} gate",
        )
        spec = _load_yaml_mapping(
            spec_path,
            f"{task_id} oracle",
        )
        lazarus = ((spec.get("evaluation") or {}).get("lazarus"))
        upstream = lazarus.get("upstream_tests") if isinstance(lazarus, Mapping) else None
        values = upstream.get("per_trial_p2p") if isinstance(upstream, Mapping) else None
        per_trial_ids[task_id] = tuple(str(value) for value in values or ())
        metadata_path = _lazarus_golden_metadata_path(spec_path, spec)
        verification = validate_lazarus_golden_set(metadata_path, task_id=task_id)
        task_evidence[task_id] = {
            "oracle_spec_sha256": _sha256(spec_path),
            "upstream_evidence_sha256": _sha256(upstream_path),
            "golden_metadata_sha256": verification.metadata_sha256 or "",
        }
        if not verification.complete:
            # Preserve a normal fail-closed preflight result (rather than a
            # late provider-time exception) while retaining the local detail.
            task_evidence[task_id]["golden_metadata_sha256"] = ""

    repo_paths = [workspace / host["repo"] for host in hosts.values()]
    sandbox = preflight_c1_sandboxes(repo_paths)
    environment = {
        "apfs": sandbox.get("filesystem") == "apfs",
        "capacity_ok": int(sandbox.get("eligible_free_bytes") or 0)
        >= int(sandbox.get("minimum_free_bytes") or 0),
        "submodules_clean": all(_is_git_clean(repo) for repo in repo_paths),
        "visual_judge_fake": bool(os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")),
        "sandbox": sandbox,
    }
    unity_editors = check_lazarus_phase0_unity_editors(workspace)
    environment["unity_editors"] = unity_editors.get("complete") is True
    environment["unity_editor_availability"] = unity_editors
    return build_lazarus_phase0_preflight(
        tasks=tasks,
        pairs=pairs,
        calibration=calibration,
        manifest_sha256=_sha256(manifest_file),
        harness_sha256=lazarus_harness_hash(workspace),
        provider_identity_sha256=lazarus_provider_identity_sha256(manifest),
        backend_paths={name: values["binary_paths"] for name, values in _BACKENDS.items()},
        capture_backends={name: str(values["capture_backend"]) for name, values in _BACKENDS.items()},
        input_backends={name: str(values["input_backend"]) for name, values in _BACKENDS.items()},
        gate_reports=gate_reports,
        per_trial_p2p_ids=per_trial_ids,
        agent_sources=views,
        task_evidence=task_evidence,
        environment=environment,
        contract=contract,
    )


def _incomplete_provenance_preflight(
    *,
    tasks: Sequence[Mapping[str, Any]],
    contract: LazarusPhase0ManifestContract,
    manifest_sha256: str,
    harness_sha256: str,
    provider_identity_sha256: str,
    task_evidence: Mapping[str, Mapping[str, str]],
    failures: Sequence[str],
) -> LazarusPhase0Preflight:
    """Build the durable preflight shape for an early provenance rejection."""

    normalized_tasks = tuple(dict(task) for task in tasks)
    task_ids = tuple(str(task.get("id") or "") for task in normalized_tasks)
    plan = build_lazarus_trial_plan(
        task_ids,
        seeds=(0,),
        model=PHASE0_DECISION_MODEL,
        formal=False,
    )
    all_failures = list(failures)
    all_failures.extend(plan.failures)
    return LazarusPhase0Preflight(
        contract=contract,
        tasks=normalized_tasks,
        plan=plan,
        pairs={},
        agent_sources={},
        expected_provenance={},
        per_trial_p2p_ids={},
        p2p_p95_s=None,
        environment={
            "preflight_stage": "upstream_provenance",
            "visual_judge_fake": bool(os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")),
        },
        manifest_sha256=manifest_sha256,
        harness_sha256=harness_sha256,
        provider_identity_sha256=provider_identity_sha256,
        task_evidence={
            str(task_id): {str(name): str(value) for name, value in values.items()}
            for task_id, values in task_evidence.items()
        },
        complete=False,
        failures=tuple(dict.fromkeys(all_failures)),
    )


def run_lazarus_phase0(
    preflight: LazarusPhase0Preflight,
    *,
    executor: Phase0Executor,
    rng: Any,
    bootstrap_iterations: int = 10_000,
) -> LazarusPhase0Run:
    """Run exactly the preflighted eight cells, serially, and stop fail-closed."""

    if os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE"):
        return _early_stop([], "Phase 0 cannot run with a fake visual judge")
    if not preflight.complete:
        return LazarusPhase0Run(
            complete=False,
            stopped_early=True,
            stop_reason="Phase 0 preflight is incomplete",
            reports=(),
            aggregate=None,
        )
    # Import lazily: the runtime module owns adapters and imports this module's
    # context type, while this coordinator must enforce its population gate
    # before it asks even a caller-supplied executor to run one cell.
    from .lazarus_runtime import (  # noqa: PLC0415 - avoids an import cycle
        LazarusPhase0RuntimeUnsupportedError,
        validate_lazarus_phase0_runtime_support,
    )

    try:
        validate_lazarus_phase0_runtime_support(preflight.tasks)
    except LazarusPhase0RuntimeUnsupportedError as error:
        return _early_stop(
            [], f"Phase 0 runtime support is incomplete: {error}"
        )
    provider_timeout_s = (
        preflight.contract.provider_timeout_s
        if preflight.contract is not None
        else C1_PROVIDER_TIMEOUT_S
    )
    if provider_timeout_s != C1_PROVIDER_TIMEOUT_S:
        return _early_stop(
            [], "Phase 0 provider timeout does not match the registered 900 seconds"
        )
    task_by_id = {str(task["id"]): task for task in preflight.tasks}
    reports: list[dict[str, Any]] = []
    tracker = LazarusStateReuseTracker()
    for key in preflight.plan.keys:
        task = task_by_id.get(key.task_id)
        pair = preflight.pairs.get(key.task_id)
        view = preflight.agent_sources.get(key.task_id)
        if task is None or pair is None or view is None:
            return _early_stop(reports, f"{key.task_id}: preflight context drift")
        artifact = pair.buggy if key.version == "buggy" else pair.fixed
        locus = str((task.get("defect") or {}).get("defect_locus") or "")
        context = LazarusPhase0CellContext(
            key=key,
            task=task,
            artifact=artifact,
            agent_source=view,
            defect_locus=locus,
            per_trial_p2p_ids=preflight.per_trial_p2p_ids.get(key.task_id, ()),
            version_switch_ms=pair.version_switch_ms,
        )
        try:
            with provider_timeout_environment(provider_timeout_s):
                raw = executor(context)
        except Exception as error:  # noqa: BLE001 - a provider/runtime error is terminal
            return _early_stop(
                reports,
                f"{key.invocation_id()}: executor raised {type(error).__name__}: {error}",
            )
        report = dict(raw) if isinstance(raw, Mapping) else {}
        reports.append(report)
        issue = _validate_trial_report(context, report)
        if issue is not None:
            return _early_stop(reports, issue)
        try:
            if key.arm == "lazarus_state" and key.version == "buggy":
                tracker.record_buggy(
                    key,
                    checkpoint_sha256=str(report["checkpoint_sha256"]),
                    actual_synthesis_calls=int(report["actual_synthesis_calls"]),
                )
            elif key.arm == "lazarus_state" and key.version == "fixed":
                reuse = tracker.record_fixed(
                    key,
                    checkpoint_sha256=str(report["checkpoint_sha256"]),
                    synthesis_cache_hit=report.get("synthesis_cache_hit") is True,
                    actual_synthesis_calls=int(report["actual_synthesis_calls"]),
                )
                if report.get("checkpoint_reuse_success") is not reuse.checkpoint_reuse_success:
                    return _early_stop(
                        reports,
                        f"{key.invocation_id()}: checkpoint reuse evidence drift",
                    )
        except (KeyError, TypeError, ValueError) as error:
            return _early_stop(reports, f"{key.invocation_id()}: {error}")

    observed_p2p_p95_s, p2p_error = _observed_phase0_p2p_p95(reports)
    if p2p_error is not None:
        return LazarusPhase0Run(
            complete=False,
            stopped_early=False,
            stop_reason=p2p_error,
            reports=tuple(reports),
            aggregate=None,
            observed_p2p_p95_s=observed_p2p_p95_s,
        )
    if observed_p2p_p95_s is None:
        return LazarusPhase0Run(
            complete=False,
            stopped_early=False,
            stop_reason="Phase 0 observed per-trial P2P duration evidence is missing",
            reports=tuple(reports),
            aggregate=None,
            observed_p2p_p95_s=None,
        )
    if observed_p2p_p95_s > PHASE0_P2P_TIMEOUT_S:
        return LazarusPhase0Run(
            complete=False,
            stopped_early=False,
            stop_reason=(
                "Phase 0 observed per-trial P2P P95 exceeds 300 seconds; "
                "shrink the subset or stop"
            ),
            reports=tuple(reports),
            aggregate=None,
            observed_p2p_p95_s=observed_p2p_p95_s,
        )

    aggregate = lazarus_from_reports(
        reports,
        expected_task_ids=[str(task["id"]) for task in preflight.tasks],
        expected_seeds=(0,),
        expected_model=PHASE0_DECISION_MODEL,
        expected_provenance=preflight.expected_provenance,
        formal=False,
        rng=rng,
        iters=int(bootstrap_iterations),
        ci=0.95,
    )
    if aggregate.get("complete") is not True:
        return LazarusPhase0Run(
            complete=False,
            stopped_early=False,
            stop_reason="Phase 0 aggregation is incomplete",
            reports=tuple(reports),
            aggregate=aggregate,
            observed_p2p_p95_s=observed_p2p_p95_s,
        )
    return LazarusPhase0Run(
        complete=True,
        stopped_early=False,
        stop_reason=None,
        reports=tuple(reports),
        aggregate=aggregate,
        observed_p2p_p95_s=observed_p2p_p95_s,
    )


def launch_lazarus_phase0(
    root: str | Path,
    *,
    report_path: str | Path,
    preflight_factory: Any | None = None,
    runtime_factory: Any | None = None,
    executor_factory: Any | None = None,
    credential_source: str | Path | None = None,
    rng: Any | None = None,
    bootstrap_iterations: int = 10_000,
) -> LazarusPhase0Launch:
    """Run the authorized eight-cell pilot through its dedicated orchestrator.

    This is intentionally not a ``p5_scale`` execution path.  It performs
    credential-scoped preflight and manifest authorization checks before
    constructing the runtime factory.  Reading the literal credential source
    makes no request; it lets preflight bind the exact Relay route that the
    runtime will use, while an incomplete population still cannot reach a
    state-synthesis or Agent-S client.
    """

    workspace = Path(root).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Lazarus Phase 0 workspace does not exist")
    if (
        isinstance(bootstrap_iterations, bool)
        or not isinstance(bootstrap_iterations, int)
        or bootstrap_iterations < 1
    ):
        raise ValueError("Lazarus Phase 0 bootstrap_iterations must be positive")
    credentials = None
    if credential_source is not None:
        candidate = Path(credential_source).expanduser()
        credentials = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace / candidate).resolve()
        )
        if workspace != credentials and workspace not in credentials.parents:
            raise ValueError("Phase 0 credential source escapes the workspace")
    credentials_context = (
        relay_environment(credentials)
        if credentials is not None
        else contextlib.nullcontext()
    )
    preflight: LazarusPhase0Preflight | None = None
    try:
        with credentials_context:
            preflight_builder = preflight_factory or preflight_lazarus_phase0
            candidate_preflight = preflight_builder(workspace)
            if not isinstance(candidate_preflight, LazarusPhase0Preflight):
                raise TypeError(
                    "Lazarus Phase 0 preflight factory returned an invalid value"
                )
            preflight = candidate_preflight
            contract = preflight.contract
            authorized = (
                contract is not None
                and contract.phase0_paid_pilot_authorized is True
                and contract.formal_paid_run_authorized is False
                and contract.phase0_trial_count == 8
            )
            if not authorized:
                run = _early_stop([], "Phase 0 manifest authorization is invalid")
            elif os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE"):
                run = _early_stop([], "Phase 0 cannot run with a fake visual judge")
            elif not preflight.complete:
                run = _early_stop([], "Phase 0 preflight is incomplete")
            elif (
                credentials is not None
                and _phase0_provider_identity_from_contract(contract)
                != preflight.provider_identity_sha256
            ):
                run = _early_stop(
                    [], "Phase 0 provider identity drift after credential activation"
                )
            else:
                with model_request_profiles_environment(
                    {contract.model: "thinking_disabled"}
                ):
                    if runtime_factory is None:
                        from .lazarus_runtime import build_lazarus_phase0_runtime_runner

                        runtime_root = (
                            workspace
                            / ".warptest-cache"
                            / "lazarus"
                            / "phase0-runtime"
                            / "runs"
                            / f"{preflight.harness_sha256[:12]}-{time.time_ns()}"
                        )
                        runtime_runner = build_lazarus_phase0_runtime_runner(
                            workspace,
                            runtime_root=runtime_root,
                            observer_model=contract.observer_model,
                        )
                    else:
                        runtime_runner = runtime_factory(workspace)
                    bound_executor_factory = (
                        executor_factory or build_lazarus_phase0_executor
                    )
                    executor = bound_executor_factory(
                        workspace, runtime_runner=runtime_runner
                    )
                    run = run_lazarus_phase0(
                        preflight,
                        executor=executor,
                        rng=rng if rng is not None else random.Random(1234),
                        bootstrap_iterations=bootstrap_iterations,
                    )
    except Exception as error:  # noqa: BLE001 - credential/runtime setup is terminal
        if preflight is None:
            raise
        run = _early_stop(
            [],
            "Phase 0 credential/runtime activation failed: "
            f"{type(error).__name__}: {error}",
        )
    assert preflight is not None
    destination = write_lazarus_phase0_run(run, preflight, report_path)
    return LazarusPhase0Launch(
        preflight=preflight,
        run=run,
        report_path=destination,
    )


def _phase0_provider_identity_from_contract(
    contract: LazarusPhase0ManifestContract,
) -> str:
    """Recompute the secret-free registered route inside credential scope."""

    decision_model = contract.model.split("@", 1)[0]
    return lazarus_provider_identity_sha256(
        {
            "api": {"provider": "relay"},
            "models": [decision_model],
            "model_request_profiles": {decision_model: "thinking_disabled"},
            "observer_model": contract.observer_model,
            "grounder_model": "decision_model",
            "judge_model": contract.judge_model,
            "provider_timeout_s": int(contract.provider_timeout_s),
        }
    )


def write_lazarus_phase0_run(
    run: LazarusPhase0Run,
    preflight: LazarusPhase0Preflight,
    path: str | Path,
) -> Path:
    """Atomically persist a Phase-0 coordinator report for later aggregation."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(run.to_report(preflight), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def write_lazarus_phase0_preflight(
    preflight: LazarusPhase0Preflight,
    path: str | Path,
) -> Path:
    """Atomically persist the pre-dispatch evidence used by a paid pilot."""

    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(preflight.to_report(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _early_stop(reports: Sequence[dict[str, Any]], reason: str) -> LazarusPhase0Run:
    return LazarusPhase0Run(
        complete=False,
        stopped_early=True,
        stop_reason=reason,
        reports=tuple(reports),
        aggregate=None,
    )


def _validate_phase0_task_shapes(
    tasks: Mapping[str, Mapping[str, Any]], failures: list[str]
) -> None:
    shapes = {
        (
            str(task.get("benchmark") or ""),
            str((task.get("defect") or {}).get("defect_locus") or ""),
        )
        for task in tasks.values()
    }
    if ("luanti", "at_goal") not in shapes:
        failures.append("Phase 0 requires the Luanti at_goal task")
    if ("openra", "on_path") not in shapes:
        failures.append("Phase 0 requires the OpenRA on_path task")


def _validate_gate_report(
    task_id: str,
    pair: LazarusVersionPair,
    report: Mapping[str, Any],
    failures: list[str],
    *,
    golden_metadata_sha256: str | None,
) -> None:
    if (
        report.get("schema_version") != 1
        or report.get("task_id") != task_id
        or report.get("status") != "passed"
        or report.get("passed") is not True
        or report.get("complete") is not True
    ):
        failures.append(f"{task_id}: construction gate is not a passed v1 report")
        return
    checks = report.get("checks")
    if not isinstance(checks, Mapping):
        failures.append(f"{task_id}: construction gate checks are missing")
    else:
        expected = {
            "fixed_f2p": True,
            "buggy_f2p": False,
            "fixed_p2p": True,
            "buggy_p2p": True,
            "provenance_verified": True,
            "fixed_before_semantic": True,
            "buggy_before_semantic": True,
            "fixed_before_visual": True,
            "buggy_before_visual": True,
            "before_equivalent": True,
            "fixed_goal_checks": True,
            "fixed_after_visual": True,
            "buggy_defect_signature": True,
            "buggy_after_visual": True,
            "fixed_defect_signature": False,
            "fixed_invariants": True,
            "buggy_invariants": True,
        }
        for name, expected_value in expected.items():
            if checks.get(name) is not expected_value:
                failures.append(f"{task_id}: construction gate check {name} drift")
    evidence = report.get("evidence")
    goldens = evidence.get("goldens") if isinstance(evidence, Mapping) else None
    if (
        golden_metadata_sha256 is None
        or not isinstance(goldens, Mapping)
        or goldens.get("metadata_sha256") != golden_metadata_sha256
    ):
        failures.append(f"{task_id}: construction gate golden metadata provenance drift")
    versions = evidence.get("versions") if isinstance(evidence, Mapping) else None
    if not isinstance(versions, Mapping):
        failures.append(f"{task_id}: construction gate version evidence is missing")
        return
    for version, artifact in (("buggy", pair.buggy), ("fixed", pair.fixed)):
        recorded = versions.get(version)
        if not isinstance(recorded, Mapping):
            failures.append(f"{task_id}/{version}: construction gate version is missing")
            continue
        if recorded.get("tree_sha256") != artifact.tree_sha256:
            failures.append(f"{task_id}/{version}: construction gate tree provenance drift")
        if recorded.get("patch_sha256") != artifact.patch_sha256:
            failures.append(f"{task_id}/{version}: construction gate patch provenance drift")


def _validate_task_evidence(
    task_id: str,
    value: Mapping[str, str] | None,
    failures: list[str],
) -> str | None:
    """Validate durable, non-secret task evidence before a paid dispatch."""

    if not isinstance(value, Mapping):
        failures.append(f"{task_id}: task evidence is missing")
        return None
    required = (
        "oracle_spec_sha256",
        "upstream_evidence_sha256",
        "golden_metadata_sha256",
    )
    result: dict[str, str] = {}
    for name in required:
        candidate = value.get(name)
        if not _is_sha256(candidate):
            failures.append(f"{task_id}: task evidence {name} is invalid")
            continue
        result[name] = str(candidate)
    if len(result) != len(required):
        return None
    return result["golden_metadata_sha256"]


def _gate_p2p_durations(
    task_id: str,
    report: Mapping[str, Any],
    expected_ids: tuple[str, ...],
    failures: list[str],
) -> list[float]:
    """Return full clone/restore/subset wall-clock durations from gate evidence.

    Individual result durations cover only the registered subprocess commands.
    ``UpstreamSubsetResult.duration_ms`` starts before the strict clone and ends
    after cleanup, and also includes checkpoint restoration when the caller
    supplied one.  Preflight must use that wrapper duration so setup work cannot
    be hidden from the paid-pilot P95 budget.
    """

    if not expected_ids:
        failures.append(f"{task_id}: per-trial P2P id list is empty")
        return []
    evidence = report.get("evidence")
    upstream = evidence.get("upstream") if isinstance(evidence, Mapping) else None
    if not isinstance(upstream, Mapping):
        failures.append(f"{task_id}: construction gate upstream evidence is missing")
        return []
    durations: list[float] = []
    for version in LAZARUS_VERSIONS:
        row = upstream.get(version)
        if not isinstance(row, Mapping):
            failures.append(f"{task_id}/{version}: per-trial P2P wrapper is missing")
            continue
        if (
            row.get("complete") is not True
            or row.get("isolation") != "apfs_cow_clone"
        ):
            failures.append(
                f"{task_id}/{version}: per-trial P2P wrapper evidence is invalid"
            )
        results = row.get("results")
        by_id = {
            str(result.get("id")): result
            for result in results or []
            if isinstance(result, Mapping) and result.get("id")
        }
        selected_command_duration_ms = 0.0
        for test_id in expected_ids:
            result = by_id.get(test_id)
            if not isinstance(result, Mapping):
                failures.append(f"{task_id}/{version}: per-trial P2P result is missing for {test_id}")
                continue
            if result.get("passed") is not True or result.get("timed_out") is not False:
                failures.append(f"{task_id}/{version}: per-trial P2P did not pass for {test_id}")
                continue
            command_duration_ms = result.get("duration_ms")
            if isinstance(command_duration_ms, bool) or not isinstance(
                command_duration_ms, (int, float)
            ):
                failures.append(f"{task_id}/{version}: per-trial P2P duration is missing for {test_id}")
                continue
            command_seconds = float(command_duration_ms) / 1000.0
            if not math.isfinite(command_seconds) or command_seconds < 0:
                failures.append(f"{task_id}/{version}: per-trial P2P duration is invalid for {test_id}")
                continue
            selected_command_duration_ms += float(command_duration_ms)
        wrapper_duration_ms = row.get("duration_ms")
        if isinstance(wrapper_duration_ms, bool) or not isinstance(
            wrapper_duration_ms, (int, float)
        ):
            failures.append(
                f"{task_id}/{version}: per-trial P2P wrapper duration is missing"
            )
            continue
        wrapper_seconds = float(wrapper_duration_ms) / 1000.0
        if not math.isfinite(wrapper_seconds) or wrapper_seconds < 0:
            failures.append(
                f"{task_id}/{version}: per-trial P2P wrapper duration is invalid"
            )
            continue
        if float(wrapper_duration_ms) < selected_command_duration_ms:
            failures.append(
                f"{task_id}/{version}: per-trial P2P wrapper duration undercounts commands"
            )
            continue
        durations.append(wrapper_seconds)
    return durations


def _lazarus_golden_metadata_path(spec_path: Path, spec: Mapping[str, Any]) -> Path:
    """Resolve the registered public before golden's metadata without escape."""

    lazarus = ((spec.get("evaluation") or {}).get("lazarus"))
    goldens = lazarus.get("goldens") if isinstance(lazarus, Mapping) else None
    before = goldens.get("before") if isinstance(goldens, Mapping) else None
    if not isinstance(before, str) or not before.strip():
        # The caller passes this to the validator, which records a standard
        # missing-evidence failure rather than accepting an inferred location.
        return spec_path.parent / "visual" / "goldens.meta.json"
    image = (spec_path.parent / before).resolve()
    if spec_path.parent != image and spec_path.parent not in image.parents:
        raise ValueError("Lazarus golden path escapes its oracle directory")
    return image.parent / "goldens.meta.json"


def _validate_trial_report(
    context: LazarusPhase0CellContext, report: Mapping[str, Any]
) -> str | None:
    """Apply the shared per-cell contract to one pilot cell."""

    issue = validate_lazarus_cell_report(
        report,
        key=context.key,
        defect_locus=context.defect_locus,
        version_switch_ms=context.version_switch_ms,
        provenance_sha256=context.artifact.tree_sha256,
        per_trial_p2p_ids=context.per_trial_p2p_ids,
        observer_model=PHASE0_OBSERVER_MODEL,
        p2p_timeout_s=PHASE0_P2P_TIMEOUT_S,
    )
    if issue is None:
        return None
    return f"{context.key.invocation_id()}: {issue}"


def _observed_phase0_p2p_p95(
    reports: Sequence[Mapping[str, Any]],
) -> tuple[float | None, str | None]:
    """Report the shared P2P P95 measurement in the pilot's vocabulary."""

    value, error = _shared_observed_p2p_p95_s(reports)
    return value, None if error is None else f"Phase 0 {error}"


def _phase0_version_request(workspace: Path, task_id: str) -> LazarusVersionRequest:
    path = workspace / "benchmarks/p5/lazarus/authoring" / f"{task_id}.version-request.json"
    payload = _load_json_mapping(path, f"{task_id} version request")
    if payload.get("task_id") != task_id:
        raise ValueError(f"{task_id}: version request task id drift")
    return LazarusVersionRequest(
        task_id=task_id,
        source_repo=_contained(workspace, str(payload["source_repo"]), f"{task_id} source"),
        fixed_revision=str(payload["fixed_revision"]),
        revert_patch=_contained(workspace, str(payload["revert_patch"]), f"{task_id} patch"),
        patch_sha256=str(payload["patch_sha256"]),
        declared_files=tuple(str(value) for value in payload["declared_files"]),
        build_class=str(payload["build_class"]),
    )


def _require_clean_source(repo: Path) -> None:
    if _git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("Lazarus agent source repository must be clean")


def _stage_clone_revision(clone: Path, revision: str) -> None:
    """Move a disposable agent-view clone onto the row's registered revision.

    The clone owns its Git directory, so this detaches only the clone and leaves
    the shared submodule untouched.  The resulting view is then verified to be
    exactly at the revision the row registers.
    """

    head = _git(clone, "rev-parse", "HEAD")
    if head != str(revision):
        _git(clone, "checkout", "--detach", "--force", str(revision))
        _git(clone, "clean", "-ffdx")
        head = _git(clone, "rev-parse", "HEAD")
    if head != str(revision):
        raise ValueError(
            f"Lazarus agent source revision mismatch: expected {revision}, found {head}"
        )


def _remove_git_ignored_paths(root: Path) -> None:
    """Remove ignored build/runtime overlays from a fresh agent-view clone."""

    arguments = (
        "ls-files",
        "-z",
        "--others",
        "--ignored",
        "--exclude-standard",
        "--directory",
    )
    for raw in _git(root, *arguments).split("\0"):
        value = raw.rstrip("/")
        if not value:
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Lazarus ignored source path is unsafe: {raw!r}")
        path = root / relative
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    if _git(root, *arguments):
        raise ValueError("Lazarus agent source still contains ignored files")


def _assert_agent_source_safe(root: Path, forbidden_tokens: Sequence[str]) -> None:
    if (root / ".git").exists():
        raise ValueError("Lazarus agent source exposes .git")
    token_bytes = {token: token.encode("utf-8") for token in forbidden_tokens}
    for path in root.rglob("*"):
        if path.is_symlink():
            resolved = path.resolve()
            if root != resolved and root not in resolved.parents:
                raise ValueError(
                    f"Lazarus agent source symlink escapes its root: {path}"
                )
        if not path.is_file():
            continue
        data = path.read_bytes()
        for token, encoded in token_bytes.items():
            if encoded in data:
                raise ValueError(
                    f"Lazarus agent source contains forbidden token {token!r}: {path}"
                )


def _is_git_clean(repo: Path) -> bool:
    try:
        return not _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    except ValueError:
        return False


def check_lazarus_phase0_unity_editors(workspace: str | Path) -> dict[str, Any]:
    """Read-only proof that both registered Unity project editors remain usable.

    The two Phase-0 tasks do not execute Unity, but the manifest retains the
    formal scheduler's Unity-canary contract.  Rechecking both exact project
    versions before paid work prevents a later formal extension from inheriting
    an unrecorded machine drift.
    """

    root = Path(workspace).expanduser().resolve()
    rows: list[dict[str, Any]] = []
    for benchmark, relative_project in _PHASE0_UNITY_PROJECTS.items():
        project = (root / relative_project).resolve()
        version = _unity_project_editor_version(project)
        candidates = _unity_editor_candidates(version)
        matched_path: str | None = None
        observed_version: str | None = None
        for candidate in candidates:
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                continue
            reported = _unity_editor_version(candidate)
            if reported == version:
                matched_path = str(candidate)
                observed_version = reported
                break
            if observed_version is None:
                observed_version = reported
        rows.append(
            {
                "benchmark": benchmark,
                "project": relative_project.as_posix(),
                "required_version": version,
                "available": matched_path is not None,
                "editor_path": matched_path,
                "editor_version": observed_version,
            }
        )
    return {
        "schema": "c1-lazarus-phase0-unity-editor-availability-v1",
        "schema_version": 1,
        "complete": bool(rows) and all(row["available"] is True for row in rows),
        "editors": rows,
    }


def _unity_project_editor_version(project: Path) -> str | None:
    version_file = project / "ProjectSettings" / "ProjectVersion.txt"
    if not version_file.is_file():
        return None
    try:
        for line in version_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("m_EditorVersion:"):
                version = line.split(":", 1)[1].strip()
                return version or None
    except OSError:
        return None
    return None


def _unity_editor_candidates(version: str | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    configured = os.environ.get("UNITY_EDITOR_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    if version:
        candidates.extend(
            (
                Path(f"/Applications/Unity/Unity-{version}/Unity.app/Contents/MacOS/Unity"),
                Path(f"/Applications/Unity/Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity"),
                Path(f"/Applications/Unity Hub/Editor/{version}/Unity.app/Contents/MacOS/Unity"),
            )
        )
    # This is the conventional standalone install path used by the pinned
    # Jynew 2020 editor.  We still probe and compare its actual version.
    candidates.append(Path("/Applications/Unity/Unity.app/Contents/MacOS/Unity"))
    unique: list[Path] = []
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if resolved not in unique:
            unique.append(resolved)
    return tuple(unique)


def _unity_editor_version(editor: Path) -> str | None:
    try:
        completed = subprocess.run(
            [str(editor), "-version"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _UNITY_EDITOR_VERSION_RE.search(
        f"{completed.stdout}\n{completed.stderr}"
    )
    return match.group(0) if match else None


def _git(repo: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *arguments],
        capture_output=True,
        text=True,
        shell=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError("Git command failed" + (f": {detail}" if detail else ""))
    return completed.stdout.strip()


def _contained(workspace: Path, value: str | Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    path = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    if workspace != path and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace")
    return path


def _load_yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _load_json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
