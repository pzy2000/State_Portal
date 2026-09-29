"""Fail-closed preflight and concurrent dispatch for the formal C1-Lazarus matrix.

This is the 360-cell counterpart of :mod:`warptest.lazarus_phase0`.  The two
deliberately share every per-cell contract: the cell context, the executor that
reloads the private oracle and launches the fresh P2P subset, and the acceptance
check in :mod:`warptest.lazarus_cell_contract`.  A formal run must never accept a
cell the pilot would have rejected, so those pieces are imported rather than
reimplemented.  What is genuinely different lives here:

* a 30-row preflight that reports *every* missing input in one pass instead of
  raising on the first, because the adapter work is delivered incrementally;
* a disk-capacity gate sized from the run's own concurrency, since a 360-cell
  run clones two trees per in-flight cell;
* group-serial, group-concurrent scheduling.  The four cells of one
  ``(task_id, seed)`` group must run in the planned order so the fixed state cell
  can reuse the buggy checkpoint, while different groups are independent;
* resume at group granularity.  A checkpoint cache is process-local, so a group
  that was interrupted after its buggy state cell has nothing left to reuse and
  is re-run whole rather than silently producing a second synthesis.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import random
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .c1_experiment import relay_environment
from .c1_provider_runtime import C1_PROVIDER_TIMEOUT_S, provider_timeout_environment
from .c1_sandbox import C1_MIN_FREE_BYTES, preflight_c1_sandboxes
from .c1_task_deadline import c1_task_deadline
from .c1_task_resources import c1_task_resource_locks
from .lazarus_aggregate import lazarus_from_reports
from .lazarus_calibration import (
    lazarus_backend_binding_from_artifact,
    lazarus_harness_hash,
    lazarus_provider_identity_sha256,
    validate_lazarus_calibration,
)
from .lazarus_catalog import LAZARUS_VERSIONS, validate_lazarus_catalog
from .lazarus_cell_contract import (
    nearest_rank_p95,
    observed_p2p_p95_s,
    validate_lazarus_cell_report,
)
from .lazarus_expansion_gate import (
    FORMAL_GATE_CHECKS,
    FORMAL_GATE_CHECKS_EXPECTED_FALSE,
)
from .lazarus_formal_build import create_formal_prebuild_callback
from .lazarus_goldens import validate_lazarus_golden_set
from .lazarus_manifest import LazarusManifestContract, validate_lazarus_manifest
from .lazarus_mining import validate_lazarus_trial_provenance
from .lazarus_openra_hotkey_probe import (
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    create_openra_pr21453_prebuild_callback,
)
from .lazarus_orchestrator import (
    DEFAULT_LAZARUS_MODEL,
    DEFAULT_LAZARUS_SEEDS,
    LazarusStateReuseTracker,
    LazarusTrialKey,
    LazarusTrialPlan,
    build_lazarus_trial_plan,
)
from .lazarus_phase0 import (
    AgentVisibleSource,
    LazarusPhase0CellContext,
    build_lazarus_phase0_executor,
    lazarus_private_source_tokens,
    prepare_lazarus_agent_source,
)
from .lazarus_roster import unbound_formal_task_ids
from .lazarus_runtime_support import (
    lazarus_runtime_support_report,
    paid_runtime_unbound_task_ids,
)
from .lazarus_versions import (
    LazarusVersionPair,
    LazarusVersionRequest,
    materialize_lazarus_versions,
)
from .model_request import model_request_profiles_environment


FORMAL_MANIFEST = Path("benchmarks/p5/lazarus_manifest.yaml")
FORMAL_CATALOG = Path("benchmarks/p5/c1_lazarus_tasks.yaml")
FORMAL_CALIBRATION = Path("reports/lazarus/formal-calibration.json")
FORMAL_AGENT_VIEW_ROOT = Path(".warptest-cache/lazarus/agent-views/formal")
FORMAL_AGENT_GIT_ROOT = Path(".warptest-cache/lazarus/agent-git/formal")
FORMAL_P2P_SANDBOX_ROOT = Path(".warptest-cache/lazarus/p2p/formal")
FORMAL_P2P_TIMEOUT_S = 300.0
FORMAL_JUDGE_MODEL = "qwen3-vl-flash@relay"
FORMAL_OBSERVER_MODEL = "claude-sonnet-5@relay"
# The manifest's per-task budget, expressed for the shared deadline context.
FORMAL_TASK_TIMEOUT_S = 2_400.0
DEFAULT_FORMAL_OUTER_WORKERS = 4
RUN_INDEX_SCHEMA = "c1-lazarus-formal-run-index-v1"

# The trusted backends each formal host is calibrated against.
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
    "jupyterlab": {
        "capture_backend": "playwright_page_screenshot_v1",
        "input_backend": "playwright_page_input_v1",
        "binary_paths": ("dev_mode/static/main.js",),
    },
}


class LazarusFormalStop(RuntimeError):
    """Raised to abandon the whole run when the harness itself is untrustworthy."""


# A cell can fail because a model did poorly, which is data, or because the
# harness lied about what it dispatched, which invalidates every other group as
# well.  Only the second kind stops the run.
_GLOBAL_INTEGRITY_FAILURES = (
    "provenance is invalid",
    "provenance SHA drift",
    "does not match the dispatched cell",
)


def _is_global_integrity_failure(issue: str) -> bool:
    return any(marker in issue for marker in _GLOBAL_INTEGRITY_FAILURES)


@dataclass(frozen=True)
class LazarusCapacityBudget:
    """The free space a 360-cell run needs beyond what it already occupies."""

    outer_workers: int
    clones_per_cell: int
    largest_host_bytes: int
    headroom_bytes: int

    @property
    def required_free_bytes(self) -> int:
        peak = self.outer_workers * self.clones_per_cell * self.largest_host_bytes
        return int(peak + self.headroom_bytes)

    def to_report(self) -> dict[str, Any]:
        return {
            "schema": "c1-lazarus-formal-capacity-v1",
            "schema_version": 1,
            "outer_workers": self.outer_workers,
            "clones_per_cell": self.clones_per_cell,
            "largest_host_bytes": self.largest_host_bytes,
            "headroom_bytes": self.headroom_bytes,
            "required_free_bytes": self.required_free_bytes,
        }


@dataclass(frozen=True)
class LazarusFormalPreflight:
    """All evidence that must exist before a 360-cell provider dispatch."""

    contract: LazarusManifestContract | None
    tasks: tuple[dict[str, Any], ...]
    plan: LazarusTrialPlan
    pairs: dict[str, LazarusVersionPair]
    agent_sources: dict[str, AgentVisibleSource]
    expected_provenance: dict[tuple[str, str], str]
    per_trial_p2p_ids: dict[str, tuple[str, ...]]
    p2p_p95_s: float | None
    capacity: LazarusCapacityBudget | None
    runtime_support: dict[str, Any]
    environment: dict[str, Any]
    manifest_sha256: str
    harness_sha256: str
    provider_identity_sha256: str
    task_evidence: dict[str, dict[str, str]]
    complete: bool
    failures: tuple[str, ...]

    def to_report(self) -> dict[str, Any]:
        """Return the durable pre-dispatch evidence without task-private data.

        As in Phase 0, this deliberately records cell identities, provenance
        bindings and environment evidence, but no issue wording, defect block,
        hidden oracle, patch path, or upstream conversation payload.
        """

        return {
            "schema": "c1-lazarus-formal-preflight-v1",
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
            "capacity": self.capacity.to_report() if self.capacity else None,
            "runtime_support": dict(self.runtime_support),
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
class LazarusFormalGroupResult:
    """The four cells of one ``(task_id, seed)`` group."""

    task_id: str
    seed: int
    reports: tuple[dict[str, Any], ...]
    complete: bool
    failures: tuple[str, ...]


@dataclass(frozen=True)
class LazarusFormalRun:
    """Serialized result of a formal attempt, including partial completion."""

    complete: bool
    stopped_early: bool
    stop_reason: str | None
    reports: tuple[dict[str, Any], ...]
    groups: tuple[LazarusFormalGroupResult, ...]
    aggregate: dict[str, Any] | None
    observed_p2p_p95_s: float | None = None
    imported_groups: tuple[tuple[str, int], ...] = ()

    def to_report(self, preflight: LazarusFormalPreflight) -> dict[str, Any]:
        return {
            "schema": "c1-lazarus-formal-run-v1",
            "schema_version": 1,
            "complete": self.complete,
            "stopped_early": self.stopped_early,
            "stop_reason": self.stop_reason,
            "expected_cells": preflight.plan.trial_count,
            "observed_cells": len(self.reports),
            "observed_p2p_p95_s": self.observed_p2p_p95_s,
            "imported_groups": [
                {"task_id": task_id, "seed": seed}
                for task_id, seed in self.imported_groups
            ],
            "groups": [
                {
                    "task_id": group.task_id,
                    "seed": group.seed,
                    "complete": group.complete,
                    "observed_cells": len(group.reports),
                    "failures": list(group.failures),
                }
                for group in self.groups
            ],
            "preflight": {
                "complete": preflight.complete,
                "failures": list(preflight.failures),
                "p2p_p95_s": preflight.p2p_p95_s,
                "capacity": (
                    preflight.capacity.to_report() if preflight.capacity else None
                ),
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
            },
            "reports": [dict(report) for report in self.reports],
            "aggregate": self.aggregate,
        }


@dataclass(frozen=True)
class LazarusFormalLaunch:
    """One formal launch and its durable coordinator artifacts."""

    preflight: LazarusFormalPreflight
    run: LazarusFormalRun
    report_path: Path
    index_path: Path


FormalExecutor = Callable[[LazarusPhase0CellContext], Mapping[str, Any]]


def formal_capacity_budget(
    root: str | Path,
    *,
    outer_workers: int,
    hosts: Mapping[str, Mapping[str, str]],
    headroom_bytes: int = C1_MIN_FREE_BYTES,
) -> LazarusCapacityBudget:
    """Size the run's peak clone footprint from the largest formal host.

    Every in-flight cell clones the versioned artifact for its runtime and again
    for its isolated P2P subset.  Clones start as copy-on-write and diverge, so
    the budget assumes a full divergence for each of them rather than trusting
    APFS to stay cheap for a multi-day run.
    """

    workspace = Path(root).expanduser().resolve()
    largest = 0
    for host in hosts.values():
        repo = (workspace / str(host.get("repo") or "")).resolve()
        if repo.is_dir():
            largest = max(largest, _directory_bytes(repo))
    return LazarusCapacityBudget(
        outer_workers=int(outer_workers),
        clones_per_cell=2,
        largest_host_bytes=largest,
        headroom_bytes=int(headroom_bytes),
    )


def build_lazarus_formal_preflight(
    *,
    tasks: Sequence[Mapping[str, Any]],
    pairs: Mapping[str, LazarusVersionPair],
    calibration: Mapping[str, Any],
    manifest_sha256: str,
    harness_sha256: str,
    provider_identity_sha256: str,
    gate_reports: Mapping[str, Mapping[str, Any]],
    per_trial_p2p_ids: Mapping[str, Sequence[str]],
    agent_sources: Mapping[str, AgentVisibleSource],
    task_evidence: Mapping[str, Mapping[str, Any]],
    environment: Mapping[str, Any],
    runtime_support: Mapping[str, Any],
    capacity: LazarusCapacityBudget | None = None,
    contract: LazarusManifestContract | None = None,
    materialization_failures: Sequence[str] = (),
) -> LazarusFormalPreflight:
    """Validate all non-provider formal inputs as one fail-closed public seam."""

    failures: list[str] = list(materialization_failures)
    normalized_tasks = tuple(dict(task) for task in tasks)
    normalized_p2p_ids = {
        str(task_id): tuple(str(value) for value in values)
        for task_id, values in per_trial_p2p_ids.items()
    }
    normalized_task_evidence = {
        str(task_id): {str(name): str(value) for name, value in record.items()}
        for task_id, record in task_evidence.items()
        if isinstance(record, Mapping)
    }
    task_by_id: dict[str, dict[str, Any]] = {}
    for task in normalized_tasks:
        task_id = str(task.get("id") or "")
        if not task_id or task_id in task_by_id:
            failures.append("formal task ids must be non-empty and unique")
            continue
        task_by_id[task_id] = task
    task_ids = tuple(task_by_id)
    # An unauthorized manifest is refused elsewhere.  Planning against the
    # registered defaults keeps the artifact's ``expected_cells`` meaningful, so
    # a reader sees the matrix that is being withheld rather than a bare zero.
    model = contract.model if contract is not None else DEFAULT_LAZARUS_MODEL
    seeds = contract.seeds if contract is not None else DEFAULT_LAZARUS_SEEDS
    plan = build_lazarus_trial_plan(
        task_ids,
        seeds=seeds,
        model=model,
        formal=True,
    )
    if not plan.complete:
        failures.extend(plan.failures)
    if contract is not None and task_ids != contract.task_ids:
        failures.append("formal task order does not match its manifest contract")

    expected_versions: dict[tuple[str, str], dict[str, Any]] = {}
    expected_provenance: dict[tuple[str, str], str] = {}
    p2p_durations_s: list[float] = []
    for task_id, task in task_by_id.items():
        benchmark = str(task.get("benchmark") or "")
        pair = pairs.get(task_id)
        if pair is None:
            # A missing pair is already reported by the materialization pass; do
            # not repeat it as a second, less specific failure.
            continue
        if benchmark not in _BACKENDS:
            failures.append(f"{task_id}: no formal backend registration for {benchmark}")
            continue
        backend = _BACKENDS[benchmark]
        for version, artifact in (("buggy", pair.buggy), ("fixed", pair.fixed)):
            if artifact.task_id != task_id or artifact.version != version:
                failures.append(
                    f"{task_id}/{version}: materialized artifact identity drift"
                )
                continue
            try:
                binding = lazarus_backend_binding_from_artifact(
                    artifact,
                    benchmark=benchmark,
                    capture_backend=str(backend["capture_backend"]),
                    input_backend=str(backend["input_backend"]),
                    binary_paths=tuple(backend["binary_paths"]),
                    canary_passed=True,
                )
            except ValueError as error:
                failures.append(
                    f"{task_id}/{version}: calibration binding failed: {error}"
                )
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
            failures.append(
                f"{task_id}: agent-visible source does not hide version identity"
            )
        golden_metadata_sha256 = _validate_task_evidence(
            task_id, normalized_task_evidence.get(task_id), failures
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

    if expected_versions:
        failures.extend(
            validate_lazarus_calibration(
                calibration,
                expected_versions=expected_versions,
                manifest_sha256=manifest_sha256,
                harness_sha256=harness_sha256,
                provider_identity_sha256=provider_identity_sha256,
            )
        )
    else:
        failures.append("formal calibration cannot be checked without a version pair")
    for name in ("apfs", "submodules_clean", "capacity_ok"):
        if environment.get(name) is not True:
            failures.append(f"formal environment check failed: {name}")
    if environment.get("visual_judge_fake") is True:
        failures.append("the formal matrix cannot run with a fake visual judge")
    if runtime_support.get("provider_dispatch_permitted") is not True:
        unbound = runtime_support.get("unbound_task_ids") or []
        failures.append(
            "these rows have no provider-backed runtime: " + ", ".join(map(str, unbound))
        )
    p95 = nearest_rank_p95(p2p_durations_s)
    if p95 is None:
        failures.append("formal per-trial P2P duration evidence is missing")
    elif p95 > FORMAL_P2P_TIMEOUT_S:
        failures.append(
            "formal per-trial P2P P95 exceeds 300 seconds; shrink the subset or stop"
        )
    return LazarusFormalPreflight(
        contract=contract,
        tasks=normalized_tasks,
        plan=plan,
        pairs=dict(pairs),
        agent_sources=dict(agent_sources),
        expected_provenance=expected_provenance,
        per_trial_p2p_ids=normalized_p2p_ids,
        p2p_p95_s=p95,
        capacity=capacity,
        runtime_support=dict(runtime_support),
        environment=dict(environment),
        manifest_sha256=manifest_sha256,
        harness_sha256=harness_sha256,
        provider_identity_sha256=provider_identity_sha256,
        task_evidence=normalized_task_evidence,
        complete=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


def preflight_lazarus_formal(
    root: str | Path = Path.cwd(),
    *,
    manifest_path: str | Path = FORMAL_MANIFEST,
    catalog_path: str | Path = FORMAL_CATALOG,
    calibration_path: str | Path = FORMAL_CALIBRATION,
    cache_root: str | Path = ".warptest-cache/lazarus",
    agent_view_root: str | Path = FORMAL_AGENT_VIEW_ROOT,
    agent_git_root: str | Path = FORMAL_AGENT_GIT_ROOT,
    outer_workers: int = DEFAULT_FORMAL_OUTER_WORKERS,
) -> LazarusFormalPreflight:
    """Materialize and verify the whole formal population without provider access.

    Unlike the pilot, this reports every missing input in one pass.  The adapter
    work lands row by row, so an operator needs the full remaining gap list, not
    the first row that happens to be incomplete.
    """

    workspace = Path(root).expanduser().resolve()
    manifest_file = _contained(workspace, manifest_path, "manifest")
    catalog_file = _contained(workspace, catalog_path, "catalog")
    cache = _contained(workspace, cache_root, "cache root")
    view_root = _contained(workspace, agent_view_root, "agent view root")
    git_root = _contained(workspace, agent_git_root, "agent Git root")
    manifest = _load_yaml_mapping(manifest_file, "Lazarus formal manifest")
    catalog = _load_yaml_mapping(catalog_file, "Lazarus formal catalog")
    hosts, tasks = validate_lazarus_catalog(catalog, source_path=catalog_file)
    runtime_support = lazarus_runtime_support_report(tasks)
    unbound = tuple(
        dict.fromkeys(
            (
                *unbound_formal_task_ids(tasks, repository_root=workspace),
                *paid_runtime_unbound_task_ids(tasks),
            )
        )
    )
    contract: LazarusManifestContract | None = None
    failures: list[str] = []
    try:
        contract = validate_lazarus_manifest(
            manifest,
            catalog_task_ids=[str(task["id"]) for task in tasks],
            unbound_task_ids=unbound,
        )
    except ValueError as error:
        failures.append(f"manifest is not authorized: {error}")

    pairs: dict[str, LazarusVersionPair] = {}
    views: dict[str, AgentVisibleSource] = {}
    gate_reports: dict[str, Mapping[str, Any]] = {}
    per_trial_ids: dict[str, tuple[str, ...]] = {}
    task_evidence: dict[str, dict[str, str]] = {}
    for task in tasks:
        task_id = str(task["id"])
        try:
            _prepare_formal_task(
                workspace,
                task,
                fixed_revision=str(
                    hosts.get(str(task.get("benchmark") or ""), {}).get("revision") or ""
                ),
                cache=cache,
                view_root=view_root,
                git_root=git_root,
                pairs=pairs,
                views=views,
                gate_reports=gate_reports,
                per_trial_ids=per_trial_ids,
                task_evidence=task_evidence,
            )
        except Exception as error:  # noqa: BLE001 - one row must not hide the rest
            failures.append(f"{task_id}: {type(error).__name__}: {error}")

    calibration_file = _contained(workspace, calibration_path, "calibration")
    try:
        calibration = _load_json_mapping(calibration_file, "Lazarus formal calibration")
    except ValueError as error:
        calibration = {}
        failures.append(str(error))

    capacity = formal_capacity_budget(
        workspace, outer_workers=outer_workers, hosts=hosts
    )
    repo_paths = [workspace / host["repo"] for host in hosts.values()]
    environment: dict[str, Any] = {
        "visual_judge_fake": bool(os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")),
        "submodules_clean": all(_is_git_clean(repo) for repo in repo_paths),
    }
    try:
        sandbox = preflight_c1_sandboxes(
            repo_paths, min_free_bytes=capacity.required_free_bytes
        )
    except Exception as error:  # noqa: BLE001 - capacity/APFS state is terminal evidence
        environment["apfs"] = False
        environment["capacity_ok"] = False
        environment["sandbox_error"] = f"{type(error).__name__}: {error}"
    else:
        environment["apfs"] = sandbox.get("filesystem") == "apfs"
        environment["capacity_ok"] = int(
            sandbox.get("eligible_free_bytes") or 0
        ) >= int(sandbox.get("minimum_free_bytes") or 0)
        environment["sandbox"] = sandbox

    return build_lazarus_formal_preflight(
        tasks=tasks,
        pairs=pairs,
        calibration=calibration,
        manifest_sha256=_sha256(manifest_file),
        harness_sha256=lazarus_harness_hash(workspace),
        provider_identity_sha256=lazarus_provider_identity_sha256(manifest),
        gate_reports=gate_reports,
        per_trial_p2p_ids=per_trial_ids,
        agent_sources=views,
        task_evidence=task_evidence,
        environment=environment,
        runtime_support=runtime_support,
        capacity=capacity,
        contract=contract,
        materialization_failures=failures,
    )


def _prepare_formal_task(
    workspace: Path,
    task: Mapping[str, Any],
    *,
    fixed_revision: str,
    cache: Path,
    view_root: Path,
    git_root: Path,
    pairs: dict[str, LazarusVersionPair],
    views: dict[str, AgentVisibleSource],
    gate_reports: dict[str, Mapping[str, Any]],
    per_trial_ids: dict[str, tuple[str, ...]],
    task_evidence: dict[str, dict[str, str]],
) -> None:
    """Materialize one row's versions, agent view and durable evidence."""

    task_id = str(task["id"])
    defect = task.get("defect")
    if not isinstance(defect, Mapping):
        raise ValueError("private defect metadata is missing")
    upstream_path = _contained(
        workspace, str(defect.get("upstream_record") or ""), "upstream record"
    )
    upstream = _load_json_mapping(upstream_path, "upstream record")
    source_root = _contained(workspace, str(task.get("repo") or ""), "source repository")
    provenance_errors = validate_lazarus_trial_provenance(
        upstream,
        source_root=source_root,
        harness_root=workspace,
        # A record's frozen test bytes are published beside it, under the row's
        # own defect directory, and its ``source_path`` values are relative to
        # that directory.  Without this root the validator cannot confirm the
        # frozen sources exist and refuses every row that declares any.
        frozen_test_root=upstream_path.parent.parent,
    )
    if provenance_errors:
        raise ValueError("upstream provenance invalid: " + "; ".join(provenance_errors))

    request = _formal_version_request(workspace, task, fixed_revision=fixed_revision)
    pairs[task_id] = materialize_lazarus_versions(
        request,
        cache_root=cache,
        build=_formal_prebuild(task_id, request.build_class),
    )
    stamp = f"{request.fixed_revision[:12]}-{time.time_ns()}"
    views[task_id] = prepare_lazarus_agent_source(
        task_id=task_id,
        source_repo=Path(request.source_repo).resolve(),
        source_revision=request.fixed_revision,
        destination=view_root / task_id / stamp,
        trusted_git_dir=git_root / task_id / f"{stamp}.git",
        forbidden_tokens=_formal_forbidden_tokens(task_id),
    )
    gate_reports[task_id] = _load_json_mapping(
        _contained(workspace, str(defect["gate_report"]), "gate"), "gate"
    )
    spec_path = _contained(workspace, str(task["oracle_spec"]), "oracle")
    spec = _load_yaml_mapping(spec_path, "oracle")
    lazarus = (spec.get("evaluation") or {}).get("lazarus")
    upstream_tests = (
        lazarus.get("upstream_tests") if isinstance(lazarus, Mapping) else None
    )
    values = (
        upstream_tests.get("per_trial_p2p")
        if isinstance(upstream_tests, Mapping)
        else None
    )
    per_trial_ids[task_id] = tuple(str(value) for value in values or ())
    verification = validate_lazarus_golden_set(
        _golden_metadata_path(spec_path, spec), task_id=task_id
    )
    task_evidence[task_id] = {
        "oracle_spec_sha256": _sha256(spec_path),
        "upstream_evidence_sha256": _sha256(upstream_path),
        "golden_metadata_sha256": verification.metadata_sha256 or "",
    }
    if not verification.complete:
        # Preserve a normal fail-closed preflight result rather than a late
        # provider-time exception, while keeping the local detail.
        task_evidence[task_id]["golden_metadata_sha256"] = ""


def run_lazarus_formal(
    preflight: LazarusFormalPreflight,
    *,
    executor: FormalExecutor,
    rng: Any,
    bootstrap_iterations: int = 10_000,
    outer_workers: int = DEFAULT_FORMAL_OUTER_WORKERS,
    resume_index: Mapping[str, Any] | None = None,
    progress_index_path: str | Path | None = None,
    task_timeout_s: float = FORMAL_TASK_TIMEOUT_S,
    lease_resources: bool = True,
) -> LazarusFormalRun:
    """Run the preflighted 360 cells and aggregate only a complete matrix."""

    if os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE"):
        return _early_stop("the formal matrix cannot run with a fake visual judge")
    if not preflight.complete:
        return _early_stop("formal preflight is incomplete")
    contract = preflight.contract
    if contract is None or contract.formal_paid_run_authorized is not True:
        return _early_stop("formal paid run is not authorized")
    if contract.provider_timeout_s != C1_PROVIDER_TIMEOUT_S:
        return _early_stop(
            "formal provider timeout does not match the registered 900 seconds"
        )
    if (
        isinstance(outer_workers, bool)
        or not isinstance(outer_workers, int)
        or outer_workers < 1
    ):
        return _early_stop("formal outer worker count must be a positive integer")

    imported, groups = _import_resumable_groups(preflight, resume_index)
    pending = [
        group for group in _plan_groups(preflight) if (group[0], group[1]) not in imported
    ]
    stop_reason: str | None = None
    global_stop = threading.Event()
    results: dict[tuple[str, int], LazarusFormalGroupResult] = {
        (group.task_id, group.seed): group for group in groups
    }
    progress_destination = (
        Path(progress_index_path).expanduser().resolve()
        if progress_index_path is not None
        else None
    )

    def ordered_results() -> tuple[LazarusFormalGroupResult, ...]:
        return tuple(
            results[identity]
            for identity in [
                (task_id, seed) for task_id, seed, _ in _plan_groups(preflight)
            ]
            if identity in results
        )

    def persist_progress() -> None:
        if progress_destination is None:
            return
        current = ordered_results()
        _write_lazarus_formal_index_groups(
            current,
            preflight,
            progress_destination,
            complete=False,
            observed_cells=sum(len(group.reports) for group in current),
        )

    def run_group(task_id: str, seed: int, keys: tuple[LazarusTrialKey, ...]):
        return _run_group(
            preflight,
            task_id=task_id,
            seed=seed,
            keys=keys,
            executor=executor,
            provider_timeout_s=contract.provider_timeout_s,
            task_timeout_s=task_timeout_s,
            lease_resources=lease_resources,
            global_stop=global_stop,
        )

    persist_progress()
    worker_count = min(outer_workers, max(len(pending), 1))
    remaining = iter(pending)
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures: dict[Future, tuple[str, int]] = {}

        def submit_next() -> bool:
            if global_stop.is_set():
                return False
            try:
                task_id, seed, keys = next(remaining)
            except StopIteration:
                return False
            future = pool.submit(run_group, task_id, seed, keys)
            futures[future] = (task_id, seed)
            return True

        for _ in range(worker_count):
            if not submit_next():
                break

        while futures:
            completed, _ = wait(tuple(futures), return_when=FIRST_COMPLETED)
            for future in completed:
                identity = futures.pop(future)
                if future.cancelled():
                    continue
                try:
                    group = future.result()
                except LazarusFormalStop as error:
                    if stop_reason is None:
                        stop_reason = str(error)
                    global_stop.set()
                    for pending_future in futures:
                        pending_future.cancel()
                    persist_progress()
                    continue
                except Exception as error:  # noqa: BLE001 - group failure is evidence
                    group = LazarusFormalGroupResult(
                        task_id=identity[0],
                        seed=identity[1],
                        reports=(),
                        complete=False,
                        failures=(f"{type(error).__name__}: {error}",),
                    )
                results[identity] = group
                persist_progress()
            while len(futures) < worker_count and submit_next():
                pass

    ordered = ordered_results()
    reports = tuple(report for group in ordered for report in group.reports)
    if stop_reason is not None:
        return LazarusFormalRun(
            complete=False,
            stopped_early=True,
            stop_reason=stop_reason,
            reports=reports,
            groups=ordered,
            aggregate=None,
            imported_groups=tuple(sorted(imported)),
        )

    observed_p95, p95_error = observed_p2p_p95_s(reports)
    incomplete = tuple(group for group in ordered if not group.complete)
    if incomplete or len(reports) != preflight.plan.trial_count:
        return LazarusFormalRun(
            complete=False,
            stopped_early=False,
            stop_reason=(
                f"{len(incomplete)} group(s) did not complete; "
                f"observed {len(reports)} of {preflight.plan.trial_count} cells"
            ),
            reports=reports,
            groups=ordered,
            aggregate=None,
            observed_p2p_p95_s=observed_p95,
            imported_groups=tuple(sorted(imported)),
        )
    if p95_error is not None:
        return LazarusFormalRun(
            complete=False,
            stopped_early=False,
            stop_reason=f"formal {p95_error}",
            reports=reports,
            groups=ordered,
            aggregate=None,
            observed_p2p_p95_s=observed_p95,
            imported_groups=tuple(sorted(imported)),
        )
    if observed_p95 is None:
        return LazarusFormalRun(
            complete=False,
            stopped_early=False,
            stop_reason="formal observed per-trial P2P duration evidence is missing",
            reports=reports,
            groups=ordered,
            aggregate=None,
            imported_groups=tuple(sorted(imported)),
        )
    if observed_p95 > FORMAL_P2P_TIMEOUT_S:
        return LazarusFormalRun(
            complete=False,
            stopped_early=False,
            stop_reason=(
                "formal observed per-trial P2P P95 exceeds 300 seconds; "
                "shrink the subset or stop"
            ),
            reports=reports,
            groups=ordered,
            aggregate=None,
            observed_p2p_p95_s=observed_p95,
            imported_groups=tuple(sorted(imported)),
        )

    aggregate = lazarus_from_reports(
        list(reports),
        expected_task_ids=[str(task["id"]) for task in preflight.tasks],
        expected_seeds=contract.seeds,
        expected_model=contract.model,
        expected_provenance=preflight.expected_provenance,
        formal=True,
        rng=rng,
        iters=int(bootstrap_iterations),
        ci=0.95,
    )
    return LazarusFormalRun(
        complete=aggregate.get("complete") is True,
        stopped_early=False,
        stop_reason=(
            None
            if aggregate.get("complete") is True
            else "formal aggregation is incomplete"
        ),
        reports=reports,
        groups=ordered,
        aggregate=aggregate,
        observed_p2p_p95_s=observed_p95,
        imported_groups=tuple(sorted(imported)),
    )


def _run_group(
    preflight: LazarusFormalPreflight,
    *,
    task_id: str,
    seed: int,
    keys: tuple[LazarusTrialKey, ...],
    executor: FormalExecutor,
    provider_timeout_s: float,
    task_timeout_s: float,
    lease_resources: bool,
    global_stop: threading.Event | None = None,
) -> LazarusFormalGroupResult:
    """Run one group's four cells in planned order under one reuse tracker.

    The order matters: ``buggy_before_fixed_state_v1`` guarantees the state arm's
    buggy cell precedes its fixed cell, which is the only way the fixed cell can
    prove it reused the same checkpoint with zero new synthesis.
    """

    task_by_id = {str(task["id"]): task for task in preflight.tasks}
    task = task_by_id.get(task_id)
    pair = preflight.pairs.get(task_id)
    view = preflight.agent_sources.get(task_id)
    if task is None or pair is None or view is None:
        return LazarusFormalGroupResult(
            task_id=task_id,
            seed=seed,
            reports=(),
            complete=False,
            failures=(f"{task_id}: preflight context drift",),
        )
    benchmark = str(task.get("benchmark") or "")
    repo = str(task.get("repo") or "")
    tracker = LazarusStateReuseTracker()
    reports: list[dict[str, Any]] = []
    failures: list[str] = []
    for key in keys:
        if global_stop is not None and global_stop.is_set():
            failures.append(f"{key.invocation_id()}: global integrity stop")
            break
        artifact = pair.buggy if key.version == "buggy" else pair.fixed
        context = LazarusPhase0CellContext(
            key=key,
            task=task,
            artifact=artifact,
            agent_source=view,
            defect_locus=str((task.get("defect") or {}).get("defect_locus") or ""),
            per_trial_p2p_ids=preflight.per_trial_p2p_ids.get(task_id, ()),
            version_switch_ms=pair.version_switch_ms,
        )
        try:
            with contextlib.ExitStack() as stack:
                if lease_resources:
                    # Queueing for a backend lease is infrastructure overhead, so
                    # the task clock starts only once every lease is held.
                    stack.enter_context(c1_task_resource_locks(repo, benchmark))
                stack.enter_context(c1_task_deadline(key.task_id, task_timeout_s))
                stack.enter_context(provider_timeout_environment(provider_timeout_s))
                raw = executor(context)
        except BaseException as error:  # noqa: BLE001 - includes the task timeout
            failures.append(
                f"{key.invocation_id()}: executor raised {type(error).__name__}: {error}"
            )
            break
        report = dict(raw) if isinstance(raw, Mapping) else {}
        reports.append(report)
        issue = validate_lazarus_cell_report(
            report,
            key=key,
            defect_locus=context.defect_locus,
            version_switch_ms=context.version_switch_ms,
            provenance_sha256=context.artifact.tree_sha256,
            per_trial_p2p_ids=context.per_trial_p2p_ids,
            observer_model=FORMAL_OBSERVER_MODEL,
            p2p_timeout_s=FORMAL_P2P_TIMEOUT_S,
        )
        if issue is not None:
            if _is_global_integrity_failure(issue):
                raise LazarusFormalStop(f"{key.invocation_id()}: {issue}")
            failures.append(f"{key.invocation_id()}: {issue}")
            break
        try:
            _record_reuse(tracker, key, report)
        except (KeyError, TypeError, ValueError) as error:
            failures.append(f"{key.invocation_id()}: {error}")
            break
    return LazarusFormalGroupResult(
        task_id=task_id,
        seed=seed,
        reports=tuple(reports),
        complete=not failures and len(reports) == len(keys),
        failures=tuple(failures),
    )


def _record_reuse(
    tracker: LazarusStateReuseTracker,
    key: LazarusTrialKey,
    report: Mapping[str, Any],
) -> None:
    if key.arm != "lazarus_state":
        return
    if key.version == "buggy":
        tracker.record_buggy(
            key,
            checkpoint_sha256=str(report["checkpoint_sha256"]),
            actual_synthesis_calls=int(report["actual_synthesis_calls"]),
        )
        return
    reuse = tracker.record_fixed(
        key,
        checkpoint_sha256=str(report["checkpoint_sha256"]),
        synthesis_cache_hit=report.get("synthesis_cache_hit") is True,
        actual_synthesis_calls=int(report["actual_synthesis_calls"]),
    )
    if report.get("checkpoint_reuse_success") is not reuse.checkpoint_reuse_success:
        raise ValueError("checkpoint reuse evidence drift")


def _plan_groups(
    preflight: LazarusFormalPreflight,
) -> list[tuple[str, int, tuple[LazarusTrialKey, ...]]]:
    """Group the planned cells by ``(task_id, seed)``, preserving cell order."""

    grouped: dict[tuple[str, int], list[LazarusTrialKey]] = {}
    order: list[tuple[str, int]] = []
    for key in preflight.plan.keys:
        identity = (key.task_id, key.seed)
        if identity not in grouped:
            grouped[identity] = []
            order.append(identity)
        grouped[identity].append(key)
    return [
        (task_id, seed, tuple(grouped[(task_id, seed)])) for task_id, seed in order
    ]


def _import_resumable_groups(
    preflight: LazarusFormalPreflight,
    resume_index: Mapping[str, Any] | None,
) -> tuple[set[tuple[str, int]], list[LazarusFormalGroupResult]]:
    """Import only groups a previous run finished in full.

    A partially finished group cannot be resumed: the state arm's checkpoint
    cache is process-local, so its fixed cell would have to synthesize a second
    checkpoint and would no longer be measuring reuse.  Such a group is dropped
    and re-run whole.
    """

    if not isinstance(resume_index, Mapping):
        return set(), []
    if resume_index.get("schema") != RUN_INDEX_SCHEMA:
        raise ValueError("Lazarus formal resume index has an unknown schema")
    if resume_index.get("manifest_sha256") != preflight.manifest_sha256:
        raise ValueError("Lazarus formal resume index was produced by another manifest")
    if resume_index.get("harness_sha256") != preflight.harness_sha256:
        raise ValueError("Lazarus formal resume index was produced by another harness")
    expected = {
        (task_id, seed): keys for task_id, seed, keys in _plan_groups(preflight)
    }
    imported: set[tuple[str, int]] = set()
    groups: list[LazarusFormalGroupResult] = []
    for row in resume_index.get("groups") or []:
        if not isinstance(row, Mapping) or row.get("status") != "ok":
            continue
        task_id = str(row.get("task_id") or "")
        seed = row.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            continue
        keys = expected.get((task_id, seed))
        if keys is None:
            raise ValueError(
                f"Lazarus formal resume index has an unregistered group {task_id}/{seed}"
            )
        cells = row.get("cells")
        if not isinstance(cells, list) or len(cells) != len(keys):
            continue
        reports: list[dict[str, Any]] = []
        for cell in cells:
            if not isinstance(cell, Mapping):
                break
            path = Path(str(cell.get("path") or ""))
            if not path.is_file():
                break
            payload = json.loads(path.read_text(encoding="utf-8"))
            if hashlib.sha256(path.read_bytes()).hexdigest() != cell.get("sha256"):
                raise ValueError(
                    f"Lazarus formal snapshot {path} does not match its recorded digest"
                )
            reports.append(dict(payload))
        if len(reports) != len(keys):
            continue
        imported.add((task_id, seed))
        groups.append(
            LazarusFormalGroupResult(
                task_id=task_id,
                seed=seed,
                reports=tuple(reports),
                complete=True,
                failures=(),
            )
        )
    return imported, groups


def launch_lazarus_formal(
    root: str | Path,
    *,
    report_path: str | Path,
    index_path: str | Path | None = None,
    resume_index_path: str | Path | None = None,
    preflight_factory: Any | None = None,
    runtime_factory: Any | None = None,
    executor_factory: Any | None = None,
    credential_source: str | Path | None = None,
    rng: Any | None = None,
    bootstrap_iterations: int = 10_000,
    outer_workers: int = DEFAULT_FORMAL_OUTER_WORKERS,
) -> LazarusFormalLaunch:
    """Run the authorized formal matrix through its dedicated orchestrator.

    This is intentionally not a ``p5_scale`` execution path.  Credential-scoped
    preflight and authorization checks happen before the runtime factory is
    constructed, so an incomplete population cannot reach a synthesis or
    Agent-S client.
    """

    workspace = Path(root).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Lazarus formal workspace does not exist")
    if (
        isinstance(bootstrap_iterations, bool)
        or not isinstance(bootstrap_iterations, int)
        or bootstrap_iterations < 1
    ):
        raise ValueError("Lazarus formal bootstrap_iterations must be positive")
    destination = Path(report_path).expanduser()
    destination = (
        destination.resolve()
        if destination.is_absolute()
        else (workspace / destination).resolve()
    )
    index_destination = (
        Path(index_path).expanduser()
        if index_path is not None
        else destination.with_name("formal-run-index.json")
    )
    index_destination = (
        index_destination.resolve()
        if index_destination.is_absolute()
        else (workspace / index_destination).resolve()
    )
    credentials = None
    if credential_source is not None:
        candidate = Path(credential_source).expanduser()
        credentials = (
            candidate.resolve()
            if candidate.is_absolute()
            else (workspace / candidate).resolve()
        )
        if workspace != credentials and workspace not in credentials.parents:
            raise ValueError("formal credential source escapes the workspace")
    resume_index: Mapping[str, Any] | None = None
    if resume_index_path is not None:
        resume_index = _load_json_mapping(
            _contained(workspace, resume_index_path, "resume index"), "resume index"
        )

    credentials_context = (
        relay_environment(credentials)
        if credentials is not None
        else contextlib.nullcontext()
    )
    preflight: LazarusFormalPreflight | None = None
    try:
        with credentials_context:
            builder = preflight_factory or preflight_lazarus_formal
            candidate_preflight = builder(workspace)
            if not isinstance(candidate_preflight, LazarusFormalPreflight):
                raise TypeError("Lazarus formal preflight factory returned an invalid value")
            preflight = candidate_preflight
            contract = preflight.contract
            if contract is None or contract.formal_paid_run_authorized is not True:
                run = _early_stop("formal manifest authorization is invalid")
            elif os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE"):
                run = _early_stop("the formal matrix cannot run with a fake visual judge")
            elif not preflight.complete:
                run = _early_stop("formal preflight is incomplete")
            elif (
                credentials is not None
                and _provider_identity_from_contract(contract)
                != preflight.provider_identity_sha256
            ):
                run = _early_stop(
                    "formal provider identity drift after credential activation"
                )
            else:
                with model_request_profiles_environment(
                    {contract.model: "thinking_disabled"}
                ):
                    if runtime_factory is None:
                        from .lazarus_runtime import build_lazarus_formal_runtime_runner

                        runtime_root = (
                            workspace
                            / ".warptest-cache"
                            / "lazarus"
                            / "formal-runtime"
                            / "runs"
                            / f"{preflight.harness_sha256[:12]}-{time.time_ns()}"
                        )
                        runtime_runner = build_lazarus_formal_runtime_runner(
                            workspace,
                            runtime_root=runtime_root,
                            observer_model=contract.observer_model,
                        )
                    else:
                        runtime_runner = runtime_factory(workspace)
                    bound_executor_factory = (
                        executor_factory or _default_executor_factory
                    )
                    executor = bound_executor_factory(
                        workspace, runtime_runner=runtime_runner
                    )
                    run = run_lazarus_formal(
                        preflight,
                        executor=executor,
                        rng=rng if rng is not None else random.Random(1234),
                        bootstrap_iterations=bootstrap_iterations,
                        outer_workers=outer_workers,
                        resume_index=resume_index,
                        progress_index_path=index_destination,
                    )
    except Exception as error:  # noqa: BLE001 - credential/runtime setup is terminal
        if preflight is None:
            raise
        run = _early_stop(
            f"formal credential/runtime activation failed: {type(error).__name__}: {error}"
        )
    assert preflight is not None
    written = write_lazarus_formal_run(run, preflight, destination)
    index = write_lazarus_formal_index(run, preflight, index_destination)
    return LazarusFormalLaunch(
        preflight=preflight,
        run=run,
        report_path=written,
        index_path=index,
    )


def _default_executor_factory(workspace: Path, *, runtime_runner: Any) -> FormalExecutor:
    """Bind the shared cell executor to the formal P2P sandbox root."""

    return build_lazarus_phase0_executor(
        workspace,
        runtime_runner=runtime_runner,
        p2p_sandbox_root=FORMAL_P2P_SANDBOX_ROOT,
        judge_model=FORMAL_JUDGE_MODEL,
    )


def _provider_identity_from_contract(contract: LazarusManifestContract) -> str:
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


def write_lazarus_formal_run(
    run: LazarusFormalRun,
    preflight: LazarusFormalPreflight,
    path: str | Path,
) -> Path:
    """Atomically persist a formal coordinator report for later aggregation."""

    return _atomic_json(Path(path), run.to_report(preflight))


def write_lazarus_formal_preflight(
    preflight: LazarusFormalPreflight, path: str | Path
) -> Path:
    """Atomically persist the pre-dispatch evidence used by a paid run."""

    return _atomic_json(Path(path), preflight.to_report())


def write_lazarus_formal_index(
    run: LazarusFormalRun,
    preflight: LazarusFormalPreflight,
    path: str | Path,
) -> Path:
    """Snapshot every completed cell and record a resumable group index.

    Each cell is written once as immutable evidence and digested, so a later
    resume can prove it is importing the same bytes this run produced.
    """

    return _write_lazarus_formal_index_groups(
        run.groups,
        preflight,
        path,
        complete=run.complete,
        observed_cells=len(run.reports),
    )


def _write_lazarus_formal_index_groups(
    run_groups: Sequence[LazarusFormalGroupResult],
    preflight: LazarusFormalPreflight,
    path: str | Path,
    *,
    complete: bool,
    observed_cells: int,
) -> Path:
    """Atomically checkpoint the currently completed formal groups."""

    destination = Path(path).expanduser().resolve()
    cells_root = destination.parent / "cells"
    groups: list[dict[str, Any]] = []
    for group in run_groups:
        rows: list[dict[str, Any]] = []
        if group.complete:
            for report in group.reports:
                invocation = _report_invocation_id(report)
                snapshot = cells_root / f"{invocation}.json"
                payload = dict(report)
                if snapshot.is_file():
                    if _load_json_mapping(snapshot, "formal cell snapshot") != payload:
                        raise ValueError(
                            f"formal cell snapshot {snapshot} is immutable and drifted"
                        )
                    written = snapshot.resolve()
                else:
                    written = _atomic_json(snapshot, payload)
                rows.append(
                    {
                        "invocation_id": invocation,
                        "path": str(written),
                        "sha256": hashlib.sha256(written.read_bytes()).hexdigest(),
                    }
                )
        groups.append(
            {
                "task_id": group.task_id,
                "seed": group.seed,
                "status": "ok" if group.complete else "failed",
                "cells": rows,
                "failures": list(group.failures),
            }
        )
    return _atomic_json(
        destination,
        {
            "schema": RUN_INDEX_SCHEMA,
            "schema_version": 1,
            "complete": complete,
            "manifest_sha256": preflight.manifest_sha256,
            "harness_sha256": preflight.harness_sha256,
            "expected_cells": preflight.plan.trial_count,
            "observed_cells": observed_cells,
            "groups": groups,
        },
    )


def _report_invocation_id(report: Mapping[str, Any]) -> str:
    key = LazarusTrialKey(
        arm=str(report.get("arm") or ""),
        model=str(report.get("model") or ""),
        seed=int(report.get("seed") or 0),
        task_id=str(report.get("task_id") or ""),
        version=str(report.get("version") or ""),
    )
    return key.invocation_id()


def _early_stop(reason: str) -> LazarusFormalRun:
    return LazarusFormalRun(
        complete=False,
        stopped_early=True,
        stop_reason=reason,
        reports=(),
        groups=(),
        aggregate=None,
    )


def _formal_version_request(
    workspace: Path,
    task: Mapping[str, Any],
    *,
    fixed_revision: str,
) -> LazarusVersionRequest:
    """Derive one row's materialization request from the registered catalog.

    The request deliberately does not come from the row's authoring
    ``version-request.json``.  That file names the full inverse patch, which also
    reverts the fix's own regression test, while the published gate graded a pair
    built from the production-only ``formal-v1`` patch.  Materializing from the
    authoring patch would therefore produce a buggy tree the gate never scored.

    ``declared_files`` is read back out of the patch instead of being restated,
    so the request cannot claim a file the patch does not touch.
    """

    task_id = str(task["id"])
    defect = task.get("defect")
    if not isinstance(defect, Mapping):
        raise ValueError("private defect metadata is missing")
    patch = _contained(workspace, str(defect["revert_patch"]), "revert patch")
    declared = _patch_touched_files(patch)
    if not declared:
        raise ValueError("registered revert patch names no files")
    return LazarusVersionRequest(
        task_id=task_id,
        source_repo=_contained(workspace, str(task["repo"]), "source"),
        fixed_revision=_registered_fixed_revision(
            workspace,
            task_id,
            patch_sha256=str(defect["patch_sha256"]),
            host_revision=fixed_revision,
        ),
        revert_patch=patch,
        patch_sha256=str(defect["patch_sha256"]),
        declared_files=declared,
        build_class=str(defect["build_class"]),
    )


def _registered_fixed_revision(
    workspace: Path,
    task_id: str,
    *,
    patch_sha256: str,
    host_revision: str,
) -> str:
    """Return the revision this row's fixed tree is materialized from.

    Many frozen rows reproduce a defect that predates the pinned submodule
    revision, so their inverse patch is anchored at the contemporaneous tree and
    is rejected by the pinned one.  ``scripts/register_lazarus_materialization_base.py``
    resolves and verifies that base once and records it beside the row; reading
    it here keeps a paid run from re-deriving it by searching history.

    A row with no sidecar falls back to the pinned revision, which is correct for
    every current-base row and fails loudly rather than silently for the rest.
    """

    path = (
        workspace
        / "benchmarks/p5/lazarus/authoring"
        / f"{task_id}.materialization.json"
    )
    if not path.is_file():
        return str(host_revision)
    payload = _load_json_mapping(path, f"{task_id} materialization base")
    if payload.get("schema") != "c1-lazarus-materialization-base-v1":
        raise ValueError("materialization base has an unknown schema")
    if payload.get("task_id") != task_id:
        raise ValueError("materialization base task id drift")
    # The base was verified against one exact patch.  If the row now registers a
    # different patch, the recorded verification no longer describes it.
    if payload.get("patch_sha256") != patch_sha256:
        raise ValueError(
            "materialization base was verified against a different revert patch"
        )
    revision = str(payload.get("fixed_revision") or "")
    if len(revision) != 40 or any(c not in "0123456789abcdef" for c in revision):
        raise ValueError("materialization base revision must be a full SHA")
    return revision


def _patch_touched_files(path: Path) -> tuple[str, ...]:
    """Return the post-image paths a unified diff writes, in first-seen order."""

    files: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("+++ "):
            continue
        value = line[4:].strip()
        if value == "/dev/null":
            continue
        # Strip the conventional destination prefix without touching a path that
        # genuinely begins with "b".
        if value.startswith("b/"):
            value = value[2:]
        if value and value not in files:
            files.append(value)
    return tuple(files)


def _formal_prebuild(task_id: str, build_class: str) -> Any:
    """Return the registered prebuild callback for one row, if it needs one.

    A ``build_class: none`` row deliberately has no prebuild step.  Any other
    row must have a registered recipe; letting an unregistered one through as
    "no build" would surface later as a confusing materialization error instead
    of naming the missing recipe.
    """

    if task_id == OPENRA_HOTKEY_PHASE0_TASK_ID:
        return create_openra_pr21453_prebuild_callback()
    if build_class == "none":
        return None
    return create_formal_prebuild_callback(task_id)


def _formal_forbidden_tokens(task_id: str) -> tuple[str, ...]:
    """Return the private tokens that must not appear in the agent-visible view."""

    return lazarus_private_source_tokens(task_id)


def _validate_task_evidence(
    task_id: str, value: Mapping[str, str] | None, failures: list[str]
) -> str | None:
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


def _validate_gate_report(
    task_id: str,
    pair: LazarusVersionPair,
    report: Mapping[str, Any],
    failures: list[str],
    *,
    golden_metadata_sha256: str | None,
) -> None:
    """Score one row's published construction gate.

    Two provenances publish a formal gate.  The frozen rows restate freeze-sprint
    Gate 1-13 evidence and carry a per-version ``versions`` block; the expansion
    rows are live receipts and carry a single ``patch`` block instead.  Both
    publish the same eighteen-check vocabulary with the same polarity, so the
    verdict is scored once here and only the provenance binding differs.
    """

    if (
        report.get("schema_version") != 1
        or report.get("task_id") != task_id
        or report.get("status") != "passed"
        or report.get("complete") is not True
    ):
        failures.append(f"{task_id}: construction gate is not a passed v1 report")
        return
    checks = report.get("checks")
    if not isinstance(checks, Mapping):
        failures.append(f"{task_id}: construction gate checks are missing")
    else:
        missing = sorted(FORMAL_GATE_CHECKS - set(checks))
        if missing:
            failures.append(
                f"{task_id}: construction gate omits check(s) {', '.join(missing)}"
            )
        for name in sorted(FORMAL_GATE_CHECKS & set(checks)):
            expected = name not in FORMAL_GATE_CHECKS_EXPECTED_FALSE
            if bool(checks.get(name)) is not expected:
                failures.append(f"{task_id}: construction gate check {name} drift")
    evidence = report.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    goldens = evidence.get("goldens")
    if (
        golden_metadata_sha256 is None
        or not isinstance(goldens, Mapping)
        or goldens.get("metadata_sha256") != golden_metadata_sha256
    ):
        failures.append(f"{task_id}: construction gate golden metadata provenance drift")
    _validate_gate_patch_provenance(task_id, pair, evidence, failures)


def _validate_gate_patch_provenance(
    task_id: str,
    pair: LazarusVersionPair,
    evidence: Mapping[str, Any],
    failures: list[str],
) -> None:
    """Bind the gate's recorded inverse patch to the materialized pair.

    Both provenances grade one pair built from one inverse patch, and
    materialization gives both versions that same ``patch_sha256``.  The frozen
    projection records it under ``versions``, the live receipt under ``patch``;
    either way the recorded digest must be the digest the pair was built from, or
    the gate's verdict describes a different buggy tree.
    """

    frozen = evidence.get("versions")
    live = evidence.get("patch")
    if isinstance(frozen, Mapping):
        recorded_sha = frozen.get("patch_sha256")
        recorded_paths = frozen.get("changed_files")
    elif isinstance(live, Mapping):
        recorded_sha = live.get("sha256")
        recorded_paths = live.get("production_paths")
    else:
        failures.append(f"{task_id}: construction gate patch provenance is missing")
        return
    if recorded_sha != pair.buggy.patch_sha256:
        failures.append(f"{task_id}: construction gate patch provenance drift")
    if not isinstance(recorded_paths, list) or not recorded_paths:
        failures.append(f"{task_id}: construction gate names no patched file")
        return
    unexpected = sorted(
        {str(value) for value in recorded_paths} - set(pair.buggy.changed_files)
    )
    if unexpected:
        failures.append(
            f"{task_id}: construction gate patched files absent from the "
            f"materialized pair: {', '.join(unexpected)}"
        )


def _gate_p2p_durations(
    task_id: str,
    report: Mapping[str, Any],
    expected_ids: tuple[str, ...],
    failures: list[str],
) -> list[float]:
    """Return full clone/restore/subset wall-clock durations from gate evidence."""

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
        if row.get("complete") is not True or row.get("isolation") != "apfs_cow_clone":
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
                failures.append(
                    f"{task_id}/{version}: per-trial P2P result is missing for {test_id}"
                )
                continue
            if result.get("passed") is not True or result.get("timed_out") is not False:
                failures.append(
                    f"{task_id}/{version}: per-trial P2P did not pass for {test_id}"
                )
                continue
            command_duration_ms = result.get("duration_ms")
            if isinstance(command_duration_ms, bool) or not isinstance(
                command_duration_ms, (int, float)
            ):
                failures.append(
                    f"{task_id}/{version}: per-trial P2P duration is missing for {test_id}"
                )
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


def _golden_metadata_path(spec_path: Path, spec: Mapping[str, Any]) -> Path:
    lazarus = (spec.get("evaluation") or {}).get("lazarus")
    goldens = lazarus.get("goldens") if isinstance(lazarus, Mapping) else None
    before = goldens.get("before") if isinstance(goldens, Mapping) else None
    if not isinstance(before, str) or not before.strip():
        return spec_path.parent / "visual" / "goldens.meta.json"
    image = (spec_path.parent / before).resolve()
    if spec_path.parent != image and spec_path.parent not in image.parents:
        raise ValueError("Lazarus golden path escapes its oracle directory")
    return image.parent / "goldens.meta.json"


def _directory_bytes(path: Path) -> int:
    total = 0
    for entry in path.rglob("*"):
        try:
            if entry.is_file() and not entry.is_symlink():
                total += entry.stat().st_size
        except OSError:
            continue
    return total


def _is_git_clean(repo: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1", "--untracked-files=all"],
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.returncode == 0 and not completed.stdout.strip()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _contained(workspace: Path, value: str | Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    path = (
        candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    )
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


__all__ = [
    "FORMAL_CALIBRATION",
    "FORMAL_CATALOG",
    "FORMAL_MANIFEST",
    "LazarusCapacityBudget",
    "LazarusFormalGroupResult",
    "LazarusFormalLaunch",
    "LazarusFormalPreflight",
    "LazarusFormalRun",
    "LazarusFormalStop",
    "build_lazarus_formal_preflight",
    "formal_capacity_budget",
    "launch_lazarus_formal",
    "preflight_lazarus_formal",
    "run_lazarus_formal",
    "write_lazarus_formal_index",
    "write_lazarus_formal_preflight",
    "write_lazarus_formal_run",
]
