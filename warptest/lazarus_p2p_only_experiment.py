"""Fail-closed execution contract for the paper-authoritative ``lzp-*`` set.

This module deliberately does not reuse the broader ``lz-*`` Lazarus
manifest.  It freezes the paper's eleven P2P-only rows, five-arm protocol,
registered decision models and seeds into the descriptive schedule.  Selection evidence
remains immutable: the schedule-scoped paid authorization is recorded beside a
run, never backfilled into a catalog gate report.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from collections import defaultdict
from contextlib import nullcontext
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from threading import Event, Lock
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .c1_lazarus_selection.p2p_only_frames import FRAME_SPECS
from .c1_lazarus_selection.p2p_only_profiles import load_profile_registry
from .lazarus_calibration import (
    lazarus_harness_hash,
    lazarus_provider_identity_sha256,
)
from .lazarus_catalog import LAZARUS_VERSIONS
from .lazarus_p2p_only_contract import (
    ARMS as LAZARUS_ARMS,
    SYNTH_ARMS,
    ROTATION_RULE as LAZARUS_ROTATION_RULE,
    AUTHORIZATION_SCOPE,
    AUTHORIZATION_ID,
    METHOD_PROTOCOL,
    REAL_START_CONSTRUCTION_MODES,
    cells_per_group_for,
    rotation_rule_for,
    validate_method_fields,
)
from .lazarus_goldens import validate_lazarus_golden_set
from .lazarus_orchestrator import (
    LazarusTrialKey,
)
from .lazarus_paper_catalog import (
    PAPER_CATALOG_PATH,
    PAPER_TASK_COUNT,
    PAPER_TASK_IDS,
    validate_paper_catalog,
)
from .lazarus_phase0 import (
    AgentVisibleSource,
    LazarusPhase0CellContext,
    prepare_lazarus_agent_source,
)
from .lazarus_versions import (
    LazarusVersionArtifact,
    LazarusVersionPair,
    tree_fingerprint,
)
from .model_request import canonical_model_name
from .c1_task_deadline import C1TaskTimeout
from .c1_sandbox import _important_available_bytes
from .lazarus_p2p_only_identity import (
    AGENT_SOURCE_SANITIZER_VERSION,
    canonical_sha256 as _scoped_canonical_sha256,
    materialization_sha256,
    scoped_identities,
)


MANIFEST_PATH = Path("benchmarks/p5/lazarus_p2p_only_formal_seed0_manifest.yaml")
CATALOG_PATH = PAPER_CATALOG_PATH
START_PROTOCOL = "real_cold_start_v1"
READINESS_SCHEMA = "c1-lazarus-p2p-only-readiness-v3"
RUN_SCHEMA = "c1-lazarus-p2p-only-run-v3"
INDEX_SCHEMA = "c1-lazarus-p2p-only-run-index-v3"
AUTHORIZATION_SCHEMA = "c1-lazarus-p2p-only-paid-authorization-v3"
EXPECTED_MODELS = (
    "gpt-5.6-terra@relay",
    "gemini-3.5-flash@relay",
    "kimi-k2.6@relay",
)
EXPECTED_PROFILES = {
    "gpt-5.6-terra": "thinking_disabled",
    "gemini-3.5-flash": "thinking_disabled",
    "kimi-k2.6": "thinking_disabled",
}
EXPECTED_TASK_COUNT = PAPER_TASK_COUNT
EXPECTED_SEEDS = (0, 1, 2)
EXPECTED_CELL_COUNT = 990
EXPECTED_GROUP_COUNT = 99
ASTRA_MODELS = ("gpt-6-astra@relay",)
ASTRA_AUTHORIZATION_SCOPE = "c1-lazarus-p2p-only-11tasks-astra-seed0-cdefg-v4"
ASTRA_AUTHORIZATION_ID = "formal_astra_110_paid_run"
FORMAL_MODELS = EXPECTED_MODELS + ASTRA_MODELS
FORMAL_AUTHORIZATION_SCOPE = "c1-lazarus-p2p-only-11tasks-4models-seed0-cdefg-v4"
FORMAL_AUTHORIZATION_ID = "formal_440_paid_run"


def registered_schedule(models, seeds):
    """Resolve only explicitly registered, complete paid schedules.

    Returns ``(profiles, authorization_scope, authorization_id, arms)``; the
    all registered schedules use the frozen C/D/E/F/G arms.
    """
    if any(type(seed) is not int for seed in seeds):
        raise ValueError("P2P-only seed must be an integer")
    if tuple(models) == FORMAL_MODELS and tuple(seeds) == (0,):
        return (
            {**EXPECTED_PROFILES, "gpt-6-astra": "thinking_disabled"},
            FORMAL_AUTHORIZATION_SCOPE,
            FORMAL_AUTHORIZATION_ID,
            LAZARUS_ARMS,
        )
    if tuple(models) == EXPECTED_MODELS and tuple(seeds) == EXPECTED_SEEDS:
        return EXPECTED_PROFILES, AUTHORIZATION_SCOPE, AUTHORIZATION_ID, LAZARUS_ARMS
    if tuple(models) == ASTRA_MODELS and tuple(seeds) == (0,):
        return (
            {"gpt-6-astra": "thinking_disabled"},
            ASTRA_AUTHORIZATION_SCOPE,
            ASTRA_AUTHORIZATION_ID,
            LAZARUS_ARMS,
        )
    raise ValueError("P2P-only schedule requires a registered model set and the frozen five-arm schedule")


# Cell traces use the single frozen five-arm method protocol.
_REGISTERED_METHOD_PROTOCOLS = frozenset({METHOD_PROTOCOL})

# A resumed tail reuses materialized runtimes instead of building the whole
# schedule, so it needs far less headroom than a cold 440-cell run.
MIN_FREE_BYTES = 35 * 1024**3


def _p2p_disk_capacity(workspace: Path) -> dict[str, Any]:
    """Use the same reclaimable macOS capacity policy as formal C1."""
    free_bytes = int(shutil.disk_usage(workspace).free)
    important_bytes = _important_available_bytes(workspace)
    eligible_bytes = important_bytes if important_bytes is not None else free_bytes
    return {
        "free_bytes": free_bytes,
        "important_usage_free_bytes": important_bytes,
        "eligible_free_bytes": eligible_bytes,
        "capacity_policy": (
            "macos_important_usage_v1"
            if important_bytes is not None
            else "ordinary_free_bytes_v1"
        ),
        "minimum_free_bytes": MIN_FREE_BYTES,
        "capacity_ok": eligible_bytes >= MIN_FREE_BYTES,
    }
# Only exact, reviewed full identities may cross a transport protocol boundary.
_RESUME_TRANSPORT_MIGRATIONS: set[tuple[str, str]] = {
    # User-authorized conditional Kimi image history backoff; the 83 prior
    # complete groups were revalidated with their original, unchanged inputs.
    ("864ba7bc35eff167b495c1c3ad0e4d3e62fbfbe18054ff69a2c3ea890d2da5ec",
     "73bab1ada1b8414c5d2bf177c9ad13d2fe4759f982ff10951e48a4be3781bb12"),
}
_RESUME_RUNTIME_MIGRATIONS: set[tuple[str, str]] = {
    # Closed Jupyter task is a negative Start; completed cells and ordered prefixes
    # retain original SHA/provenance and actor output under the unchanged schedule.
    ('1c35fa659198c5e457ba5558760e44c108a479f62fecfa68f5a6d8d58493d991', '0b6e3ab5aa9fe0c9de8d82f0682eb6c80fbe86e9f497448ee41ee3c66c2ac92d'),
    # Astra capture recovery: only unchanged JupyterLab groups were complete.
    ('29b98de26bf2804d158abbd2f38f6dd0bbad334b938fe12c225b006dc585a81d', 'd9d1fffaf9f90be28b99e9d2414c628df45d78445474a76c37ac5efb59b70444'),
    ('f20d0a7fa8fc0c9b14d02e9b26fb5831ea12cab8e97853f94d5a7d3aca79923d', 'cb52ea5c84859d90e34d04c4becfc8fa670bd5a6e7a6d0a6a359d5bf9f995b05'),
}
# Exact old/new grading and orchestrator identities, validated against immutable cells.
_RESUME_GRADING_MIGRATIONS: set[tuple[str, str, str, str]] = {
    ('5de070e653124d1bb12ce5495ffbdbbfccdea37669e98e8c564ce69facf31bba', 'a2353555741cbee16cd785156eeb7afc9afdb855cc751dea47ef956fc72ac6e7', 'd01e30758adda64282f68c0f425e6ca5095053b70f97d0d6718d867f0579c070', '279df425608d5f837bfe5b50989c85c920cbb3f274a490916aaf17883e33db9a'),
    ('e0aab66968a2605a8b6e780b6f6b9f7da2f493e6adbf88f32119bc30933e8546', '0cf50d022151544b2e6a127ceeaa696c5cef4ea0050ef93ceb38bc1175cd3326', 'd567a8cb8747c6377de3ffe1f7e944311ed09d8f65db8db4dceaebdbd8dbaed5', 'd567a8cb8747c6377de3ffe1f7e944311ed09d8f65db8db4dceaebdbd8dbaed5'),
    ('e55ba724096de30f0827d86ab4b5f1c3a0f55de443126a7be4ba050544d79660', 'e0aab66968a2605a8b6e780b6f6b9f7da2f493e6adbf88f32119bc30933e8546', '4ffd39cd2529a2b214498d10a6047ac1cbde215c1d9113b5ffc00868eb924a73', 'e7d30d2c4a37dafa828b97f8ce493fa24c2c41cf5422806ddb36076056e5e615'),
    # formal-component-state-440: the trial validator now shares the upstream
    # ScaleCUA checkpoint kind, and unbilled capture timeouts became retryable.
    # Exactly one of that run's 255 cells carried a scalecua_from_start receipt
    # and it stayed incomplete, so no imported cell grades differently.
    ('d314ca969bed745fa0c4fa94c55c49d0097a0bad0a1f48770c7942f21edc076b',
     'ee479566e51d576ccc692bc4b17c4ddb8a4dc72ce9f6804f798e60bc6c4cc59a',
     '8fcf241a7d0a999df2c23bffe35bebe80ee235c5423614114fcef44f19fa9fda',
     '8fcf241a7d0a999df2c23bffe35bebe80ee235c5423614114fcef44f19fa9fda'),
    # Local runtime stalls with complete accounting became retryable. The edit
    # is confined to failure recovery in the executor; no grading input, rubric
    # or oracle changed, so every imported cell keeps its original verdict.
    ('ee479566e51d576ccc692bc4b17c4ddb8a4dc72ce9f6804f798e60bc6c4cc59a',
     '240e5fcd5925dae9152448b8dd4b2c2aae9c214570064088bc02bc793aef5162',
     '8fcf241a7d0a999df2c23bffe35bebe80ee235c5423614114fcef44f19fa9fda',
     '8fcf241a7d0a999df2c23bffe35bebe80ee235c5423614114fcef44f19fa9fda'),
}
_RESUME_VALIDATOR_MIGRATIONS = {
    ('e7d30d2c4a37dafa828b97f8ce493fa24c2c41cf5422806ddb36076056e5e615', 'd567a8cb8747c6377de3ffe1f7e944311ed09d8f65db8db4dceaebdbd8dbaed5'): 'explicit_unknown_usage_group_quarantine_v1',
    (
        "d2b5d08ffded9ddfbdf9c146bc6c6896847505698446ac2f7543cdb997cc23e2",
        "fd3b3a4ae5ccf3c8d083ac576b3342e15faadb6bc9c543af75dc346ce6d8bc4f",
    ): "per_cell_materialized_provenance_validation_v1",
}


@dataclass(frozen=True)
class P2POnlyManifestContract:
    task_source: str
    catalog_sha256: str
    task_ids: tuple[str, ...]
    models: tuple[str, ...]
    model_request_profiles: dict[str, str]
    seeds: tuple[int, ...]
    arms: tuple[str, ...]
    versions: tuple[str, ...]
    observer_model: str
    grounder_model: str
    judge_model: str
    provider_timeout_s: float
    task_timeout_s: float
    per_trial_p2p_timeout_s: float
    outer_workers: int
    expected_cells: int
    authorization_scope: str
    paid_run_authorized: bool
    start_protocol: str
    total_decision_steps: int
    cold_start_bindings: dict[str, str]
    method_config: dict[str, Any]
    formal: bool = False


@dataclass(frozen=True)
class P2POnlyPlan:
    keys: tuple[LazarusTrialKey, ...]
    groups: tuple[tuple[LazarusTrialKey, ...], ...]
    complete: bool
    failures: tuple[str, ...]

    @property
    def trial_count(self) -> int:
        return len(self.keys)


@dataclass(frozen=True)
class P2POnlyReadiness:
    contract: P2POnlyManifestContract | None
    plan: P2POnlyPlan
    complete: bool
    failures: tuple[str, ...]
    manifest_sha256: str
    catalog_sha256: str
    harness_sha256: str
    provider_identity_sha256: str
    scoped: dict[str, Any]
    task_evidence: dict[str, dict[str, Any]]
    runtime_bindings: dict[str, dict[str, Any]]
    environment: dict[str, Any]
    tasks: tuple[dict[str, Any], ...]
    pairs: dict[str, LazarusVersionPair]
    agent_sources: dict[str, AgentVisibleSource]

    def to_report(self) -> dict[str, Any]:
        return {
            "schema": READINESS_SCHEMA,
            "schema_version": 3,
            "complete": self.complete,
            "failures": list(self.failures),
            "formal": bool(self.contract and self.contract.formal),
            "confirmatory": False,
            "paper_ready": False,
            "expected_cells": self.plan.trial_count,
            "method_protocol": METHOD_PROTOCOL,
            "expected_groups": len(self.plan.groups),
            "identity": {
                "identity_version": "scoped-v1",
                "schedule_sha256": self.scoped["schedule_sha256"],
                "catalog_sha256": self.catalog_sha256,
                "provider_identity_sha256": self.provider_identity_sha256,
                "provider_profile_sha256": self.scoped["provider_profile_sha256"],
                "runtime_sha256": self.scoped["runtime_sha256"],
                "grading_sha256": self.scoped["grading_sha256"],
                "orchestrator_sha256": self.scoped["orchestrator_sha256"],
                "treatment_runtime_sha256": self.scoped["treatment_runtime_sha256"],
                "materialization_sha256": self.scoped["materialization_sha256"],
                "start_protocol": START_PROTOCOL,
            },
            "presentation_identity": {},
            "legacy_diagnostic_identity": {
                "manifest_sha256": self.manifest_sha256,
                "harness_sha256": self.harness_sha256,
            },
            "plan": {
                "rotation_rule": LAZARUS_ROTATION_RULE,
                "group_identity": ["task_id", "model", "seed"],
                "cells": [_key_payload(key) for key in self.plan.keys],
            },
            "task_evidence": self.task_evidence,
            "runtime_bindings": self.runtime_bindings,
            "environment": self.environment,
        }


@dataclass(frozen=True)
class P2POnlyGroupResult:
    task_id: str
    model: str
    seed: int
    reports: tuple[dict[str, Any], ...]
    complete: bool
    failures: tuple[str, ...]
    imported: bool = False
    retryable: bool = False


@dataclass(frozen=True)
class P2POnlyRun:
    groups: tuple[P2POnlyGroupResult, ...]
    reports: tuple[dict[str, Any], ...]
    complete: bool
    failures: tuple[str, ...]
    imported_groups: tuple[tuple[str, str, int], ...]


def validate_p2p_only_manifest(
    manifest: Mapping[str, Any],
    *,
    catalog_path: Path,
    task_ids: Sequence[str],
) -> P2POnlyManifestContract:
    """Validate the exact approved paper schedule."""

    if manifest.get("schema_version") != 3:
        raise ValueError("P2P-only manifest schema_version must be 3")
    if manifest.get("experiment_id") != "c1_lazarus_p2p_only":
        raise ValueError("P2P-only manifest experiment_id is invalid")
    if manifest.get("task_schema") != "c1_lazarus_v1":
        raise ValueError("P2P-only manifest task_schema is invalid")
    expected_formal = tuple(manifest.get("models") or ()) == FORMAL_MODELS
    if manifest.get("formal") is not expected_formal:
        raise ValueError("P2P-only formal status differs from the registered schedule")
    if manifest.get("confirmatory") is not False:
        raise ValueError("P2P-only experiment must be non-confirmatory")
    if manifest.get("paper_ready") is not False:
        raise ValueError("P2P-only experiment cannot be paper-ready")

    normalized_ids = tuple(str(value) for value in task_ids)
    if (
        normalized_ids != PAPER_TASK_IDS
        or len(set(normalized_ids)) != EXPECTED_TASK_COUNT
    ):
        raise ValueError(
            "P2P-only catalog must contain the frozen ordered 11-task paper population"
        )
    if manifest.get("task_count") != EXPECTED_TASK_COUNT:
        raise ValueError("P2P-only manifest task_count must be 11")
    task_source = str(manifest.get("tasks") or "")
    if task_source != CATALOG_PATH.as_posix():
        raise ValueError("P2P-only manifest must use the registered catalog")
    observed_catalog_sha = _sha256(catalog_path)
    catalog_sha = str(manifest.get("catalog_sha256") or "")
    if catalog_sha != observed_catalog_sha:
        raise ValueError("P2P-only catalog SHA-256 drift")

    models = tuple(manifest.get("models") or ())
    seeds = tuple(manifest.get("seeds") or ())
    expected_profiles, authorization_scope, authorization_id, schedule_arms = (
        registered_schedule(models, seeds)
    )
    cells_per_group = cells_per_group_for(schedule_arms)
    expected_cells = (
        len(normalized_ids) * len(models) * len(seeds) * cells_per_group
    )
    profiles = manifest.get("model_request_profiles")
    if profiles != expected_profiles:
        raise ValueError("P2P-only models must all use thinking_disabled")
    api = manifest.get("api")
    if not isinstance(api, Mapping) or api.get("provider") != "relay":
        raise ValueError("P2P-only provider must be Relay")
    if tuple(manifest.get("arms") or ()) != schedule_arms:
        raise ValueError(
            "P2P-only arms must be C, D, E for the legacy schedules or "
            "C, D, E, F, G for the formal schedule"
        )
    if tuple(manifest.get("versions") or ()) != LAZARUS_VERSIONS:
        raise ValueError("P2P-only versions must be buggy then fixed")
    if manifest.get("expected_cells") != expected_cells:
        raise ValueError(f"P2P-only expected_cells must be {expected_cells}")
    if manifest.get("start_protocol") != START_PROTOCOL:
        raise ValueError("P2P-only start protocol must be real_cold_start_v1")
    if manifest.get("decision_budget") != {
        "total_steps": 100,
        "sharing": "phase_a_then_phase_b_remaining",
    }:
        raise ValueError("P2P-only Phase A/B decision budget is invalid")
    cold_start_bindings = manifest.get("cold_start_bindings")
    expected_bindings = {
        "jupyterlab": "real_lab_launcher_benchmark_extension_v1",
        "luanti": "native_neutral_main_menu_v1",
        "openra": "native_post_boot_main_menu_v1",
    }
    if cold_start_bindings != expected_bindings:
        raise ValueError("P2P-only cold-start bindings are invalid")
    if manifest.get("jupyter_benchmark_extension") != {
        "id": "stateportal-lazarus-p2p-only-start",
        "contract": "visible_launcher_entry_opens_candidate_component_only_v1",
    }:
        raise ValueError("P2P-only Jupyter benchmark extension is invalid")
    if manifest.get("observer_model") != "claude-sonnet-5@relay":
        raise ValueError("P2P-only observer model is invalid")
    if manifest.get("grounder_model") != "decision_model":
        raise ValueError("P2P-only grounder must follow the decision model")
    if manifest.get("judge_model") != "qwen3-vl-flash@relay":
        raise ValueError("P2P-only judge model is invalid")
    expected_outer_workers = 8 if models == FORMAL_MODELS and seeds == (0,) else 2
    for field, expected in (
        ("provider_timeout_s", 900),
        ("task_timeout_s", 2400),
        ("per_trial_p2p_timeout_s", 300),
        ("outer_workers", expected_outer_workers),
    ):
        if manifest.get(field) != expected:
            raise ValueError(f"P2P-only {field} must be {expected}")
    scheduler = manifest.get("scheduler")
    if scheduler != {
        "rotation_rule": rotation_rule_for(schedule_arms),
        "group_identity": ["task_id", "model", "seed"],
        "cells_per_group": cells_per_group,
    }:
        raise ValueError("P2P-only scheduler contract is invalid")
    statistics = manifest.get("statistics")
    if statistics != {
        "mode": "exploratory_descriptive_by_model",
        "h2": "not_applicable_all_tasks_at_goal",
    }:
        raise ValueError("P2P-only statistics contract is invalid")
    authorization = manifest.get("authorization")
    if not isinstance(authorization, Mapping):
        raise ValueError("P2P-only paid authorization is missing")
    if authorization.get("scope") != authorization_scope:
        raise ValueError("P2P-only paid authorization scope is invalid")
    if type(authorization.get(authorization_id)) is not bool or set(authorization) != {
        "scope",
        authorization_id,
    }:
        raise ValueError("P2P-only v3 authorization is invalid")
    validate_method_fields(dict(manifest), arms=schedule_arms)

    return P2POnlyManifestContract(
        formal=expected_formal,
        task_source=task_source,
        catalog_sha256=catalog_sha,
        task_ids=normalized_ids,
        models=models,
        model_request_profiles=dict(expected_profiles),
        seeds=seeds,
        arms=schedule_arms,
        versions=LAZARUS_VERSIONS,
        observer_model=str(manifest["observer_model"]),
        grounder_model=str(manifest["grounder_model"]),
        judge_model=str(manifest["judge_model"]),
        provider_timeout_s=900.0,
        task_timeout_s=2400.0,
        per_trial_p2p_timeout_s=300.0,
        outer_workers=expected_outer_workers,
        expected_cells=expected_cells,
        authorization_scope=authorization_scope,
        paid_run_authorized=authorization[authorization_id],
        start_protocol=START_PROTOCOL,
        total_decision_steps=100,
        cold_start_bindings=dict(expected_bindings),
        method_config={
            name: manifest[name]
            for name in (
                "method_protocol",
                "methods",
                "shared_synthesis",
                "start_gate",
                "cost_accounting",
            )
        },
    )


def build_p2p_only_plan(
    task_ids: Sequence[str],
    *,
    models: Sequence[str] = EXPECTED_MODELS,
    seeds: Sequence[int] = EXPECTED_SEEDS,
) -> P2POnlyPlan:
    """Build cell groups keyed by ``(task_id, model, seed)``.

    Each group runs every scheduled arm over both versions (buggy rotation
    order, then the reverse fixed order); the formal v4 schedule carries ten
    cells per group, the legacy v3 schedules six.
    """

    tasks = tuple(str(value) for value in task_ids)
    selected_models = tuple(str(value) for value in models)
    selected_seeds = tuple(seeds)
    failures: list[str] = []
    if tasks != PAPER_TASK_IDS or len(set(tasks)) != EXPECTED_TASK_COUNT:
        failures.append(
            "P2P-only plan requires the frozen ordered 11-task paper population"
        )
    schedule_arms = LAZARUS_ARMS
    try:
        _, _, _, schedule_arms = registered_schedule(selected_models, selected_seeds)
    except ValueError as error:
        failures.append(str(error))
    cells_per_group = cells_per_group_for(schedule_arms)
    expected_groups = len(tasks) * len(selected_models) * len(selected_seeds)
    expected_cells = expected_groups * cells_per_group

    keys: list[LazarusTrialKey] = []
    if not failures:
        arm_cycle = len(schedule_arms)
        for model_index, model in enumerate(selected_models):
            for task_index, task in enumerate(tasks):
                offset = (model_index + task_index) % arm_cycle
                order = schedule_arms[offset:] + schedule_arms[:offset]
                for seed in selected_seeds:
                    for version, arms in (
                        ("buggy", order),
                        ("fixed", tuple(reversed(order))),
                    ):
                        keys.extend(
                            LazarusTrialKey(arm, model, seed, task, version) for arm in arms
                        )

    grouped: dict[tuple[str, str, int], list[LazarusTrialKey]] = defaultdict(list)
    for key in keys:
        grouped[(key.task_id, key.model, key.seed)].append(key)
    groups = tuple(tuple(grouped[identity]) for identity in sorted(grouped))
    if len(keys) != expected_cells or len(set(keys)) != expected_cells:
        failures.append(f"P2P-only planner did not produce {expected_cells} unique cells")
    if len(groups) != expected_groups or any(
        len(group) != cells_per_group for group in groups
    ):
        failures.append(
            f"P2P-only planner did not produce {expected_groups} "
            f"{cells_per_group}-cell groups"
        )
    for group in groups:
        state = [key for key in group if key.arm == "lazarus_state"]
        if [key.version for key in state] != ["buggy", "fixed"]:
            failures.append("P2P-only State groups must run buggy before fixed")
            break
    return P2POnlyPlan(
        keys=tuple(keys),
        groups=groups,
        complete=not failures,
        failures=tuple(dict.fromkeys(failures)),
    )


def preflight_p2p_only(
    root: str | Path = Path.cwd(),
    *,
    manifest_path: str | Path = MANIFEST_PATH,
    catalog_path: str | Path = CATALOG_PATH,
    require_runtime_canaries: bool = True,
    runtime_canary_path: str | Path | None = None,
    api_preflight_path: str | Path | None = None,
    calibration_path: str | Path | None = None,
    materialize: bool = True,
) -> P2POnlyReadiness:
    """Validate every non-paid input and previously generated online proof."""

    workspace = Path(root).expanduser().resolve()
    manifest_file = _contained(workspace, manifest_path, "manifest")
    catalog_file = _contained(workspace, catalog_path, "catalog")
    manifest = _yaml_mapping(manifest_file, "P2P-only manifest")
    scoped = scoped_identities(workspace, manifest)
    catalog = _yaml_mapping(catalog_file, "P2P-only catalog")
    hosts, tasks = validate_paper_catalog(
        catalog,
        repository_root=workspace,
        source_path=catalog_file,
    )
    task_ids = tuple(str(task["id"]) for task in tasks)
    failures: list[str] = []
    contract: P2POnlyManifestContract | None = None
    try:
        contract = validate_p2p_only_manifest(
            manifest, catalog_path=catalog_file, task_ids=task_ids
        )
    except ValueError as error:
        failures.append(f"manifest is invalid: {error}")
    plan = build_p2p_only_plan(
        task_ids,
        models=contract.models if contract is not None else EXPECTED_MODELS,
        seeds=contract.seeds if contract is not None else EXPECTED_SEEDS,
    )
    failures.extend(plan.failures)

    profiles = {
        profile.task_id: profile for profile in load_profile_registry(workspace)
    }
    task_evidence: dict[str, dict[str, Any]] = {}
    runtime_bindings: dict[str, dict[str, Any]] = {}
    pairs: dict[str, LazarusVersionPair] = {}
    agent_sources: dict[str, AgentVisibleSource] = {}
    for task in tasks:
        task_id = str(task["id"])
        evidence: dict[str, Any] = {}
        task_evidence[task_id] = evidence
        if str((task.get("defect") or {}).get("defect_locus") or "") != "at_goal":
            failures.append(f"{task_id}: P2P-only paper set must be at_goal")
        defect = task.get("defect")
        if not isinstance(defect, Mapping):
            failures.append(f"{task_id}: defect metadata is missing")
            continue
        gate_path = _contained(workspace, str(defect.get("gate_report") or ""), "gate")
        try:
            gate = _json_mapping(gate_path, f"{task_id} gate")
        except ValueError as error:
            failures.append(str(error))
            continue
        evidence["gate_sha256"] = _sha256(gate_path)
        evidence["gate_status"] = gate.get("status")
        gate_status = gate.get("gate_1_6_status")
        gate_ok = (
            gate.get("status") == "passed"
            and gate.get("complete") is True
            and gate.get("track") == "c1_lazarus_p2p_only_v1"
            and gate.get("upstream_f2p") is False
            and gate.get("paid_actions_authorized") is False
            and gate.get("formal_trials_authorized") is False
            and isinstance(gate_status, Mapping)
            and gate_status.get("fixed_buggy_p2p_3x") == "passed"
            and gate_status.get("f2p_absence_confirmed") == "passed_no_f2p"
        )
        if not gate_ok:
            failures.append(f"{task_id}: immutable Gate 1-6 evidence is invalid")
        metadata_value = (
            ((gate.get("evidence") or {}).get("goldens") or {}).get("metadata")
            if isinstance(gate.get("evidence"), Mapping)
            else None
        )
        if not isinstance(metadata_value, str):
            failures.append(f"{task_id}: golden metadata path is missing")
        else:
            metadata = _contained(workspace, metadata_value, "golden metadata")
            verification = validate_lazarus_golden_set(metadata, task_id=task_id)
            evidence["golden_metadata_sha256"] = verification.metadata_sha256
            evidence["goldens_complete"] = verification.complete
            if not verification.complete:
                failures.extend(f"{task_id}: {item}" for item in verification.failures)
        try:
            spec_path = _contained(
                workspace, str(task.get("oracle_spec") or ""), "oracle spec"
            )
            spec = _yaml_mapping(spec_path, f"{task_id} oracle")
            lazarus = (spec.get("evaluation") or {}).get("lazarus")
            upstream = (
                lazarus.get("upstream_tests") if isinstance(lazarus, Mapping) else None
            )
            per_trial = (
                upstream.get("per_trial_p2p") if isinstance(upstream, Mapping) else None
            )
            if not isinstance(per_trial, list) or not per_trial:
                raise ValueError("registered per-trial P2P ids are missing")
            evidence["oracle_spec_sha256"] = _sha256(spec_path)
            evidence["per_trial_p2p_ids"] = [str(value) for value in per_trial]
        except ValueError as error:
            failures.append(f"{task_id}: {error}")
        profile = profiles.get(task_id)
        frame = FRAME_SPECS.get(task_id)
        if profile is None or frame is None:
            failures.append(f"{task_id}: runtime profile or frame spec is missing")
            continue
        if profile.catalog_admission is not True:
            failures.append(
                f"{task_id}: profile is not admitted to the P2P-only catalog"
            )
        binding = {
            "task_id": task_id,
            "benchmark": str(task.get("benchmark")),
            "capture_kind": frame.capture_kind,
            "state_action_interface": "registered_public_transition_v1",
            "agent_s_action_interface": "pixel_mouse_keyboard_only",
            "online_runtime_required": True,
            "canary_passed": False,
        }
        runtime_bindings[task_id] = binding
        if materialize:
            try:
                fixed, buggy, pair_evidence, cached_provenance = (
                    _materialize_profile_pair(workspace, profile)
                )
                pair = _version_pair_from_profile(
                    profile=profile,
                    fixed=fixed,
                    buggy=buggy,
                    pair_evidence=pair_evidence,
                    cached_provenance=cached_provenance,
                )
                pairs[task_id] = pair
                evidence["version_provenance"] = {
                    "fixed": pair.fixed.tree_sha256,
                    "buggy": pair.buggy.tree_sha256,
                }
                agent_sources[task_id] = _cached_agent_source(
                    workspace=workspace,
                    task_id=task_id,
                    source_repo=workspace / str(task["repo"]),
                    source_revision=profile.pinned_revision,
                    forbidden_tokens=(task_id,),
                )
            except Exception as error:  # noqa: BLE001 - one row must not hide others
                failures.append(
                    f"{task_id}: materialization failed: {type(error).__name__}: {error}"
                )

    canary = _optional_artifact(workspace, runtime_canary_path, "runtime canary")
    if canary is not None:
        _bind_runtime_canary(
            canary,
            runtime_bindings,
            task_ids,
            failures,
            expected_runtime_identities=scoped["runtime_sha256"],
        )
    elif require_runtime_canaries:
        failures.append("P2P-only online runtime canary artifact is missing")

    api_preflight = _optional_artifact(workspace, api_preflight_path, "API preflight")
    if api_preflight is None:
        failures.append("P2P-only three-model API preflight artifact is missing")
    else:
        _validate_api_preflight(
            api_preflight,
            failures,
            expected_provider_profile_sha256=scoped["provider_profile_sha256"],
            models=contract.models if contract else EXPECTED_MODELS,
        )

    calibration = _optional_artifact(workspace, calibration_path, "calibration")
    if calibration is None:
        failures.append("P2P-only calibration artifact is missing")
    else:
        if calibration.get("schema") != "c1-lazarus-p2p-only-calibration-v2":
            failures.append("P2P-only calibration schema is invalid")
        if calibration.get("start_protocol") != START_PROTOCOL:
            failures.append("P2P-only calibration Start protocol is invalid")
        if calibration.get("complete") is not True:
            failures.append("P2P-only calibration is incomplete")

    repo_paths = [workspace / str(host["repo"]) for host in hosts.values()]
    environment = {
        "submodules_clean": all(_git_clean(path) for path in repo_paths),
        "visual_judge_fake": bool(os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")),
        **_p2p_disk_capacity(workspace),
    }
    if environment["submodules_clean"] is not True:
        failures.append("P2P-only source repositories are not clean")
    if environment["visual_judge_fake"] is True:
        failures.append("P2P-only paid run cannot use a fake visual judge")
    if environment["capacity_ok"] is not True:
        failures.append(
            "P2P-only run has less than "
            f"{MIN_FREE_BYTES // 1024**3} GiB eligible capacity"
        )

    provider_identity = ""
    try:
        provider_identity = lazarus_provider_identity_sha256(manifest)
    except ValueError as error:
        failures.append(f"provider identity is invalid: {error}")
    if api_preflight is not None:
        if api_preflight.get("provider_identity_sha256") != provider_identity:
            failures.append("P2P-only API preflight provider identity drift")
    if calibration is not None:
        identity = calibration.get("identity")
        if not isinstance(identity, Mapping):
            failures.append("P2P-only calibration identity is missing")
        else:
            expected_identity = {
                "schedule_sha256": scoped["schedule_sha256"],
                "catalog_sha256": _sha256(catalog_file),
                "provider_identity_sha256": provider_identity,
                "provider_profile_sha256": scoped["provider_profile_sha256"],
                "runtime_sha256": scoped["runtime_sha256"],
                "grading_sha256": scoped["grading_sha256"],
                "orchestrator_sha256": scoped["orchestrator_sha256"],
                "treatment_runtime_sha256": scoped["treatment_runtime_sha256"],
                "materialization_sha256": scoped["materialization_sha256"],
                "start_protocol": START_PROTOCOL,
            }
            for name, expected in expected_identity.items():
                if identity.get(name) != expected:
                    failures.append(f"P2P-only calibration {name} drift")
    readiness = P2POnlyReadiness(
        contract=contract,
        plan=plan,
        complete=not failures,
        failures=tuple(dict.fromkeys(failures)),
        manifest_sha256=_sha256(manifest_file),
        catalog_sha256=_sha256(catalog_file),
        harness_sha256=lazarus_harness_hash(workspace),
        provider_identity_sha256=provider_identity,
        scoped=scoped,
        task_evidence=task_evidence,
        runtime_bindings=runtime_bindings,
        environment=environment,
        tasks=tuple(dict(task) for task in tasks),
        pairs=pairs,
        agent_sources=agent_sources,
    )
    return readiness


def build_paid_authorization(
    readiness: P2POnlyReadiness,
    *,
    granted_at: str,
    local_user_id: int | None = None,
) -> dict[str, Any]:
    if readiness.contract is None or not readiness.contract.paid_run_authorized:
        raise ValueError("Selected paid schedule is not authorized")
    if not readiness.complete:
        raise ValueError("cannot authorize an incomplete P2P-only readiness")
    if not isinstance(granted_at, str) or not granted_at.endswith("Z"):
        raise ValueError("paid authorization granted_at must be UTC")
    return {
        "schema": AUTHORIZATION_SCHEMA,
        "schema_version": 3,
        "authorization_id": registered_schedule(readiness.contract.models, readiness.contract.seeds)[2],
        "scope": readiness.contract.authorization_scope,
        "granted_at": granted_at,
        "local_user_id": os.getuid() if local_user_id is None else local_user_id,
        "revoked": False,
        "formal": readiness.contract.formal,
        "confirmatory": False,
        "paper_ready": False,
        "expected_cells": readiness.plan.trial_count,
        "identity": readiness.to_report()["identity"],
        "readiness_sha256": _canonical_sha256(readiness.to_report()),
    }


def summarize_costs(
    rows: Sequence[Mapping[str, Any]], *, attributed: bool
) -> dict[str, Any]:
    totals = {
        phase: {"duration_ms": 0, "total_tokens": 0, "llm_calls": 0}
        for phase in (
            "synthesis",
            "restore",
            "execution",
            "bootstrap",
            "grading",
            "p2p",
        )
    }
    synthesis = {}
    for row in rows:
        trace = (row.get("runtime") or {}).get("trace") or {}
        for phase, value in (trace.get("costs") or {}).items():
            if phase not in totals:
                continue
            if phase == "synthesis" and value.get("synthesis_id"):
                identity = value["synthesis_id"]
                shared = value["attributed" if attributed else "shared_actual"]
                if identity in synthesis:
                    if synthesis[identity] != shared:
                        raise ValueError("shared synthesis cost drift")
                    continue
                synthesis[identity] = shared
                value = shared
            totals[phase]["duration_ms"] += value.get("duration_ms", 0)
            usage = value.get("usage") or {}
            for field in ("total_tokens", "llm_calls"):
                totals[phase][field] += usage.get(field) or 0
    return {
        "phases": totals,
        "synthesis_events": len(synthesis),
        "basis": "method_attributed" if attributed else "actual_deduplicated",
    }


def aggregate_p2p_only_reports(
    reports: Sequence[Mapping[str, Any]],
    *,
    plan: P2POnlyPlan,
    formal: bool = False,
) -> dict[str, Any]:
    """Produce descriptive, per-model summaries without confirmatory pooling."""

    expected = {key.identity() for key in plan.keys}
    index: dict[tuple[str, str, int, str, str], Mapping[str, Any]] = {}
    issues: list[str] = []
    for position, report in enumerate(reports):
        try:
            identity = (
                str(report["arm"]),
                str(report["model"]),
                int(report["seed"]),
                str(report["task_id"]),
                str(report["version"]),
            )
        except (KeyError, TypeError, ValueError):
            issues.append(f"report[{position}] has invalid identity")
            continue
        if identity in index:
            issues.append(f"duplicate cell {identity!r}")
            continue
        if identity not in expected:
            issues.append(f"unexpected cell {identity!r}")
            continue
        index[identity] = report
        if report.get("semantic_evidence") != "not_applicable_p2p_only":
            issues.append(f"{identity!r}: semantic evidence mode is invalid")
        if report.get("defect_evidence_source") != "dual_after_visual_goldens":
            issues.append(f"{identity!r}: defect evidence source is invalid")
        if report.get("complete") is not True:
            issues.append(f"{identity!r}: trial is incomplete")
        if report.get("start_protocol") != START_PROTOCOL:
            issues.append(f"{identity!r}: real Start protocol is missing")
        if report.get("start_evaluated") is not True:
            issues.append(f"{identity!r}: Start was not evaluated")
    for identity in sorted(expected - set(index), key=str):
        issues.append(f"missing cell {identity!r}")

    plan_arms = tuple(dict.fromkeys(key.arm for key in plan.keys)) or LAZARUS_ARMS
    by_model: dict[str, Any] = {}
    for model in dict.fromkeys(key.model for key in plan.keys):
        rows = [report for identity, report in index.items() if identity[1] == model]
        per_arm: dict[str, Any] = {}
        for arm in plan_arms:
            arm_rows = [row for row in rows if row.get("arm") == arm]
            start_rows = [row for row in arm_rows if row.get("start_evaluated") is True]
            starts = [row.get("start_success") is True for row in start_rows]
            fixed = {
                (str(row.get("task_id")), int(row["seed"])): row
                for row in arm_rows
                if row.get("version") == "fixed"
            }
            buggy = {
                (str(row.get("task_id")), int(row["seed"])): row
                for row in arm_rows
                if row.get("version") == "buggy"
            }
            paired = sorted(set(fixed) & set(buggy))
            discrimination = sum(
                buggy[task].get("defect_exposed") is True
                and fixed[task].get("goal_success") is True
                for task in paired
            )
            per_arm[arm] = {
                "cells": len(arm_rows),
                "start_successes": sum(starts),
                "start_evaluated": len(start_rows),
                "start_not_evaluated": len(arm_rows) - len(start_rows),
                "start_rate": sum(starts) / len(start_rows) if start_rows else None,
                "discrimination_successes": discrimination,
                "discrimination_pairs": len(paired),
                "fixed_false_alarms": sum(
                    row.get("false_alarm") is True for row in fixed.values()
                ),
            }
        for arm, summary in per_arm.items():
            summary["costs"] = summarize_costs(
                [row for row in rows if row.get("arm") == arm], attributed=True
            )
        fixed_state = [
            row
            for row in rows
            if row.get("arm") in SYNTH_ARMS and row.get("version") == "fixed"
        ]
        by_model[model] = {
            "expected_cells": sum(key.model == model for key in plan.keys),
            "observed_cells": len(rows),
            "arms": per_arm,
            "actual_costs": summarize_costs(rows, attributed=False),
            "checkpoint_reuse": {
                "successes": sum(
                    row.get("checkpoint_reuse_success") is True
                    and row.get("synthesis_cache_hit") is True
                    and row.get("actual_synthesis_calls") == 0
                    for row in fixed_state
                ),
                "n": len(fixed_state),
            },
        }
    seeds = sorted({key.seed for key in plan.keys})
    if len(seeds) > 1:
        for seed in seeds:
            seed_plan = P2POnlyPlan(
                keys=tuple(key for key in plan.keys if key.seed == seed),
                groups=tuple(group for group in plan.groups if group[0].seed == seed),
                complete=plan.complete,
                failures=plan.failures,
            )
            seed_summary = aggregate_p2p_only_reports(
                [row for row in reports if row.get("seed") == seed], plan=seed_plan, formal=formal
            )
            for model in dict.fromkeys(key.model for key in plan.keys):
                by_model[model].setdefault("seeds", {})[str(seed)] = seed_summary["models"][model]
    return {
        "schema": "c1-lazarus-p2p-only-aggregate-v3",
        "schema_version": 3,
        "complete": not issues,
        "formal": formal,
        "confirmatory": False,
        "paper_ready": False,
        "expected_cells": len(expected),
        "observed_cells": len(index),
        "issues": list(dict.fromkeys(issues)),
        "statistics": {
            "mode": "exploratory_descriptive_by_model",
            "pooled_across_models": False,
            "h2": "not_applicable_all_tasks_at_goal",
        },
        "models": by_model,
    }


def run_p2p_only_groups(
    readiness: P2POnlyReadiness,
    *,
    executor: Any,
    outer_workers: int | None = None,
    resume_index: Mapping[str, Any] | None = None,
    progress_index_path: str | Path | None = None,
    quarantined_groups: Sequence[tuple[str, str, int]] = (),
) -> P2POnlyRun:
    """Run concurrent model-scoped groups; each group's five-arm order stays fixed."""

    if not readiness.complete:
        return P2POnlyRun(
            groups=(),
            reports=(),
            complete=False,
            failures=("P2P-only readiness is incomplete",),
            imported_groups=(),
        )
    expected_workers = readiness.contract.outer_workers
    if outer_workers is None:
        outer_workers = expected_workers
    if isinstance(outer_workers, bool) or not isinstance(outer_workers, int):
        raise ValueError("P2P-only outer_workers must be an integer")
    if outer_workers != expected_workers:
        raise ValueError(f"P2P-only schedule requires outer_workers={expected_workers}")
    task_by_id = {str(task["id"]): task for task in readiness.tasks}
    resumed = _load_resume_groups(readiness, resume_index, include_partial=True)
    imported = {identity: group for identity, group in resumed.items() if group.complete}
    prior_cells = {
        LazarusTrialKey(row["arm"], row["model"], row["seed"], row["task_id"], row["version"]): row
        for group in resumed.values() for row in group.reports
    }
    results: dict[tuple[str, str, int], P2POnlyGroupResult] = {
        identity: group for identity, group in imported.items()
    }
    quarantine = set(quarantined_groups)
    planned_groups = {(group[0].task_id, group[0].model, group[0].seed) for group in readiness.plan.groups}
    if not quarantine <= planned_groups:
        raise ValueError("quarantine contains an unregistered group")
    pending = [
        group
        for group in readiness.plan.groups
        if (group[0].task_id, group[0].model, group[0].seed) not in results
        and (group[0].task_id, group[0].model, group[0].seed) not in quarantine
    ]

    from .lazarus_p2p_only_recovery import classify_failure
    from .run_context import safe_error_text
    stop_dispatch = Event()
    deferred_models = set()
    recovery_lock = Lock()

    def execute_group(keys: tuple[LazarusTrialKey, ...]) -> P2POnlyGroupResult:
        first = keys[0]
        identity = (first.task_id, first.model, first.seed)
        task = task_by_id.get(first.task_id)
        pair = readiness.pairs.get(first.task_id)
        source = readiness.agent_sources.get(first.task_id)
        if task is None or pair is None or source is None:
            return P2POnlyGroupResult(
                *identity,
                reports=(),
                complete=False,
                failures=("readiness execution context is missing",),
            )
        shared_checkpoint = None
        produced = False
        reports: list[dict[str, Any]] = []
        failures: list[str] = []
        retryable = False
        for key in keys:
            if stop_dispatch.is_set():
                failures.append("dispatch stopped after incomplete infrastructure evidence")
                break
            artifact = pair.buggy if key.version == "buggy" else pair.fixed
            context = LazarusPhase0CellContext(
                key=key,
                task=task,
                artifact=artifact,
                agent_source=source,
                defect_locus="at_goal",
                per_trial_p2p_ids=_per_trial_p2p_ids(readiness, key.task_id),
                version_switch_ms=pair.version_switch_ms,
            )
            try:
                from .c1_task_deadline import c1_task_deadline

                resource_scope = getattr(
                    executor, "resource_scope", lambda context: nullcontext()
                )
                # Waiting for the exclusive native device is queue time, not a task budget.
                if key in prior_cells:
                    raw = prior_cells[key]
                else:
                    with resource_scope(context):
                        if stop_dispatch.is_set():
                            failures.append("dispatch stopped after incomplete infrastructure evidence")
                            break
                        with c1_task_deadline(
                            key.invocation_id(), readiness.contract.task_timeout_s
                        ):
                            raw = executor(context)
                report = dict(raw) if isinstance(raw, Mapping) else {}
                issue = _validate_p2p_only_cell(context, report)
                if issue is not None:
                    if report.get("complete") is False:
                        reports.append(report)
                    error = ValueError(issue)
                    error.recovery = report.get("infrastructure_failure")
                    raise error
                if key.arm in SYNTH_ARMS:
                    checkpoint = str(report["checkpoint_sha256"])
                    calls = int(report["actual_synthesis_calls"])
                    if shared_checkpoint is None:
                        shared_checkpoint = checkpoint
                    elif checkpoint != shared_checkpoint:
                        raise ValueError("D/E shared checkpoint drift")
                    if key.version == "fixed" and calls:
                        raise ValueError("fixed D/E must not synthesize")
                    if calls:
                        if produced:
                            raise ValueError(
                                "shared synthesis was produced more than once"
                            )
                        produced = True
                    if key.version == "fixed" and not report.get("synthesis_cache_hit"):
                        raise ValueError("fixed D/E must reuse synthesis")
                reports.append(report)
            except (Exception, C1TaskTimeout) as error:  # noqa: BLE001 - incomplete group is resumable
                disposition = classify_failure(error)
                retryable = disposition["retryable"]
                if retryable:
                    if disposition.get("category") == "provider_transient":
                        with recovery_lock:
                            deferred_models.add(key.model)
                else:
                    stop_dispatch.set()
                failures.append(f"{key.invocation_id()}: {safe_error_text(error)}")
                break
        return P2POnlyGroupResult(
            *identity,
            reports=tuple(reports),
            complete=len(reports) == len(keys) and not failures,
            failures=tuple(failures),
            retryable=retryable,
        )

    def persist() -> None:
        if progress_index_path is None:
            return
        ordered = [results[identity] for identity in sorted(results)]
        write_run_index(
            [report for group in ordered for report in group.reports],
            plan=readiness.plan,
            readiness=readiness,
            path=progress_index_path,
        )

    if pending:
        with ThreadPoolExecutor(max_workers=outer_workers) as pool:
            remaining = iter(pending)
            futures = {}
            def submit_next():
                group = next(remaining, None)
                while group is not None and group[0].model in deferred_models:
                    group = next(remaining, None)
                if group is not None and not stop_dispatch.is_set():
                    futures[pool.submit(execute_group, group)] = group
            for _ in range(outer_workers):
                submit_next()
            while futures:
                done, _ = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    futures.pop(future)
                    group = future.result()
                    results[(group.task_id, group.model, group.seed)] = group
                    if not group.complete and not group.retryable:
                        stop_dispatch.set()
                    persist()
                for _ in done:
                    submit_next()
    ordered_groups = tuple(results[identity] for identity in sorted(results))
    reports = tuple(report for group in ordered_groups for report in group.reports)
    failures = tuple(failure for group in ordered_groups for failure in group.failures)
    return P2POnlyRun(
        groups=ordered_groups,
        reports=reports,
        complete=(
            len(ordered_groups) == len(readiness.plan.groups)
            and all(group.complete for group in ordered_groups)
            and len(reports) == readiness.plan.trial_count
        ),
        failures=failures,
        imported_groups=tuple(sorted(imported)),
    )


def write_readiness(readiness: P2POnlyReadiness, path: str | Path) -> Path:
    return _atomic_json(Path(path), readiness.to_report())


def write_run_index(
    reports: Sequence[Mapping[str, Any]],
    *,
    plan: P2POnlyPlan,
    readiness: P2POnlyReadiness,
    path: str | Path,
) -> Path:
    destination = Path(path).expanduser().resolve()
    cells_root = destination.parent / "cells"
    by_identity = {
        (
            str(row.get("task_id")),
            str(row.get("model")),
            int(row.get("seed", -1)),
            str(row.get("arm")),
            str(row.get("version")),
        ): dict(row)
        for row in reports
        if isinstance(row, Mapping)
    }
    groups: list[dict[str, Any]] = []
    for group in plan.groups:
        first = group[0]
        cells: list[dict[str, Any]] = []
        for key in group:
            payload = by_identity.get(
                (key.task_id, key.model, key.seed, key.arm, key.version)
            )
            if payload is None:
                continue
            cell_path = cells_root / f"{key.invocation_id()}.json"
            if cell_path.is_file():
                existing = _json_mapping(cell_path, "run-index cell")
                if existing != payload:
                    raise ValueError(f"immutable cell snapshot drift: {cell_path}")
            else:
                _atomic_json(cell_path, payload)
            cells.append(
                {
                    "identity": _key_payload(key),
                    "path": str(cell_path.resolve()),
                    "sha256": _sha256(cell_path),
                }
            )
        groups.append(
            {
                "task_id": first.task_id,
                "model": first.model,
                "seed": first.seed,
                "complete": len(cells) == len(group) and all(
                    by_identity[(key.task_id, key.model, key.seed, key.arm, key.version)].get("complete") is True
                    for key in group
                ),
                "cells": cells,
            }
        )
    payload = {
        "schema": INDEX_SCHEMA,
        "schema_version": 3,
        "complete": len(reports) == readiness.plan.trial_count
        and all(group["complete"] for group in groups),
        "expected_cells": readiness.plan.trial_count,
        "observed_cells": len(reports),
        "group_identity": ["task_id", "model", "seed"],
        "formal": bool(readiness.contract and readiness.contract.formal),
        "confirmatory": False,
        "paper_ready": False,
        "identity": readiness.to_report()["identity"],
        "groups": groups,
    }
    return _atomic_json(destination, payload)


def write_run_report(
    reports: Sequence[Mapping[str, Any]],
    *,
    plan: P2POnlyPlan,
    readiness: P2POnlyReadiness,
    authorization: Mapping[str, Any],
    path: str | Path,
) -> Path:
    aggregate = aggregate_p2p_only_reports(reports, plan=plan, formal=bool(readiness.contract and readiness.contract.formal))
    payload = {
        "schema": RUN_SCHEMA,
        "schema_version": 3,
        "start_protocol": START_PROTOCOL,
        "complete": aggregate["complete"],
        "formal": bool(readiness.contract and readiness.contract.formal),
        "confirmatory": False,
        "paper_ready": False,
        "expected_cells": readiness.plan.trial_count,
        "observed_cells": len(reports),
        "method_protocol": METHOD_PROTOCOL,
        "h2": "not_applicable_all_tasks_at_goal",
        "identity": readiness.to_report()["identity"],
        "authorization": dict(authorization),
        "aggregate": aggregate,
        "reports": [dict(report) for report in reports],
    }
    return _atomic_json(Path(path), payload)


def _load_resume_groups(
    readiness: P2POnlyReadiness,
    resume_index: Mapping[str, Any] | None,
    *,
    include_partial: bool = False,
) -> dict[tuple[str, str, int], P2POnlyGroupResult]:
    if resume_index is None:
        return {}
    if resume_index.get("schema") != INDEX_SCHEMA:
        raise ValueError("P2P-only resume index schema is invalid")
    resume_identity = resume_index.get("identity")
    current_identity = readiness.to_report()["identity"]
    grading_migration = False
    transport_migration = (_canonical_sha256(resume_identity), _canonical_sha256(current_identity)) in _RESUME_TRANSPORT_MIGRATIONS
    runtime_migration = (_canonical_sha256(resume_identity), _canonical_sha256(current_identity)) in _RESUME_RUNTIME_MIGRATIONS
    if resume_identity != current_identity:
        if not isinstance(resume_identity, Mapping):
            raise ValueError("P2P-only resume index identity drift")
        old_orchestrator = str(resume_identity.get("orchestrator_sha256") or "")
        new_orchestrator = str(current_identity.get("orchestrator_sha256") or "")
        old_scientific = dict(resume_identity)
        new_scientific = dict(current_identity)
        old_scientific.pop("orchestrator_sha256", None)
        new_scientific.pop("orchestrator_sha256", None)
        grading_migration = (
            str(old_scientific.get("grading_sha256")),
            str(new_scientific.get("grading_sha256")),
            old_orchestrator,
            new_orchestrator,
        ) in _RESUME_GRADING_MIGRATIONS
        if grading_migration:
            old_scientific.pop("grading_sha256", None)
            new_scientific.pop("grading_sha256", None)
        if not (runtime_migration or transport_migration) and (
            old_scientific != new_scientific
            or not (grading_migration or (old_orchestrator, new_orchestrator) in _RESUME_VALIDATOR_MIGRATIONS)
        ):
            raise ValueError("P2P-only resume index identity drift")
    imported: dict[tuple[str, str, int], P2POnlyGroupResult] = {}
    for row in resume_index.get("groups") or []:
        if not isinstance(row, Mapping) or (row.get("complete") is not True and not include_partial):
            continue
        if not row.get("cells"):
            continue
        identity = (
            str(row.get("task_id")),
            str(row.get("model")),
            int(row.get("seed", -1)),
        )
        if identity in imported:
            raise ValueError(f"duplicate P2P-only resume group {identity!r}")
        reports: list[dict[str, Any]] = []
        for cell in row.get("cells") or []:
            if not isinstance(cell, Mapping):
                raise ValueError("P2P-only resume cell is invalid")
            path = Path(str(cell.get("path") or "")).expanduser().resolve()
            if not path.is_file() or _sha256(path) != cell.get("sha256"):
                raise ValueError(f"P2P-only resume cell SHA drift: {path}")
            report = _json_mapping(path, "P2P-only resume cell")
            if report.get("complete") is not True and row.get("complete") is not True and include_partial:
                break
            task_id = str(report.get("task_id") or "")
            version = str(report.get("version") or "")
            pair = readiness.pairs.get(task_id)
            artifact = (
                pair.buggy
                if pair is not None and version == "buggy"
                else pair.fixed
                if pair is not None and version == "fixed"
                else None
            )
            if (
                artifact is None
                or report.get("provenance_sha256") != artifact.tree_sha256
            ):
                raise ValueError(
                    f"P2P-only resume cell materialized provenance drift: {path}"
                )
            if report.get("start_protocol") != START_PROTOCOL:
                raise ValueError(f"P2P-only resume cell Start protocol drift: {path}")
            if grading_migration or runtime_migration or transport_migration:
                from .lazarus_trial import _validate_bound_judge_model
                if report.get("complete") is not True or not isinstance(report.get("visual"), Mapping):
                    raise ValueError("grading migration requires complete visual evidence")
                audits = report["visual"].get("audits") or {}
                for version_name in ("fixed", "buggy"):
                    audit = audits.get(version_name)
                    if isinstance(audit, Mapping):
                        _validate_bound_judge_model(audit, expected="qwen3-vl-flash", label="imported " + version_name)
                if report.get("start_success") is True and not isinstance(audits.get("fixed"), Mapping):
                    raise ValueError("grading migration is missing the evaluated visual audit")
            if runtime_migration or transport_migration:
                from types import SimpleNamespace
                context = SimpleNamespace(key=LazarusTrialKey(report["arm"],report["model"],report["seed"],report["task_id"],report["version"]), artifact=artifact)
                issue = _validate_p2p_only_cell(context, report)
                if issue is not None or (runtime_migration and report.get("runtime",{}).get("trace",{}).get("policy_exit")):
                    raise ValueError("runtime compatibility does not cover this cell")
            reports.append(report)
        expected_keys = {
            key for key in readiness.plan.keys
            if (key.task_id, key.model, key.seed) == identity
        }
        actual_keys = [
            LazarusTrialKey(row["arm"], row["model"], row["seed"], row["task_id"], row["version"])
            for row in reports
        ]
        group_complete = len(reports) == len(expected_keys) and set(actual_keys) == expected_keys
        if not group_complete:
            expected_order = next((list(group) for group in readiness.plan.groups
                                   if (group[0].task_id, group[0].model, group[0].seed) == identity), [])
            if row.get("complete") is True or not include_partial or actual_keys != expected_order[:len(actual_keys)]:
                raise ValueError(f"P2P-only resume group is not atomic or an ordered complete prefix: {identity!r}")
            if not reports:
                continue
        imported[identity] = P2POnlyGroupResult(
            *identity,
            reports=tuple(reports),
            complete=group_complete,
            failures=(),
            imported=True,
        )
    return imported


def _validate_p2p_only_cell(
    context: LazarusPhase0CellContext, report: Mapping[str, Any]
) -> str | None:
    key = context.key
    for field, expected in (
        ("task_id", key.task_id),
        ("model", key.model),
        ("seed", key.seed),
        ("arm", key.arm),
        ("version", key.version),
    ):
        if report.get(field) != expected:
            return f"cell {field} identity drift"
    if report.get("complete") is not True:
        return "cell evidence is incomplete"
    if report.get("semantic_evidence") != "not_applicable_p2p_only":
        return "cell semantic evidence mode is invalid"
    if report.get("defect_evidence_source") != "dual_after_visual_goldens":
        return "cell defect evidence source is invalid"
    if report.get("provenance_sha256") != context.artifact.tree_sha256:
        return "cell provenance SHA drift"
    if report.get("start_protocol") != START_PROTOCOL:
        return "cell real Start protocol is invalid"
    if report.get("start_evaluated") is not True:
        return "cell Start evaluation is missing"
    if report.get("start_construction_mode") not in REAL_START_CONSTRUCTION_MODES:
        return "cell Start construction mode is invalid"
    runtime = report.get("runtime")
    if not isinstance(runtime, Mapping) or not runtime.get("initial_screenshot_sha256"):
        return "cell raw initial provenance is missing"
    trace = runtime.get("trace")
    if (
        not isinstance(trace, Mapping)
        or trace.get("method_protocol") not in _REGISTERED_METHOD_PROTOCOLS
    ):
        return "cell method protocol is invalid"
    gate = trace.get("start_gate")
    phase_b = trace.get("phase_b")
    if not isinstance(gate, Mapping) or gate.get("passed") is not report.get(
        "start_success"
    ):
        return "cell pre-transition Start gate is missing or disagrees with grading"
    if not isinstance(phase_b, Mapping) or (
        gate.get("passed") is not True and phase_b.get("executed") is not False
    ):
        return "cell executed Phase B before passing Start"
    if (
        key.arm in SYNTH_ARMS
        and report.get("start_construction_mode")
        != "shared_generated_state_from_cold_start"
    ):
        return "D/E did not restore generated state"
    phase_costs = trace.get("costs")
    if not isinstance(phase_costs, Mapping) or any(
        name not in phase_costs
        for name in ("synthesis", "restore", "execution", "bootstrap", "grading")
    ):
        return "cell phase costs are missing"
    if any(
        isinstance(value, Mapping)
        and isinstance(value.get("usage"), Mapping)
        and value["usage"].get("usage_complete") is False
        for value in phase_costs.values()
    ):
        return "cell phase usage is incomplete"
    if (
        report.get("per_trial_p2p_ok") is not True
        and report.get("start_success") is not False
    ):
        return "cell P2P evidence is missing or failed"
    return None


def _per_trial_p2p_ids(readiness: P2POnlyReadiness, task_id: str) -> tuple[str, ...]:
    evidence = readiness.task_evidence.get(task_id, {})
    values = evidence.get("per_trial_p2p_ids")
    return tuple(str(value) for value in values) if isinstance(values, list) else ()


def _bind_runtime_canary(
    canary: Mapping[str, Any],
    bindings: dict[str, dict[str, Any]],
    task_ids: Sequence[str],
    failures: list[str],
    *,
    expected_runtime_identities: Mapping[str, str],
) -> None:
    if canary.get("schema") != "c1-lazarus-p2p-only-runtime-canary-v3":
        failures.append("P2P-only runtime canary schema is invalid")
        return
    canary_cells = cells_per_group_for(LAZARUS_ARMS) * len(task_ids)
    if (
        canary.get("method_protocol") != METHOD_PROTOCOL
        or canary.get("expected_cells") != canary_cells
    ):
        failures.append("P2P-only canary method/coverage contract is invalid")
    if canary.get("start_protocol") != START_PROTOCOL:
        failures.append("P2P-only runtime canary Start protocol is invalid")
    if canary.get("complete") is not True:
        failures.append("P2P-only runtime canary is incomplete")
    if canary.get("runtime_identities") != dict(expected_runtime_identities):
        failures.append("P2P-only runtime canary scoped identity drift")
    rows = canary.get("tasks")
    if not isinstance(rows, list):
        failures.append("P2P-only runtime canary tasks are missing")
        return
    index = {str(row.get("task_id")): row for row in rows if isinstance(row, Mapping)}
    for task_id in task_ids:
        row = index.get(task_id)
        benchmark = str(bindings[task_id].get("benchmark") or "")
        passed = bool(
            isinstance(row, Mapping)
            and row.get("complete") is True
            and row.get("same_live_lineage") is True
            and row.get("state_transition_executed") is True
            and row.get("agent_s_pixel_transition_executed") is True
            and row.get("synth_agent_s_pixel_transition_executed") is True
            and row.get("scalecua_pixel_transition_executed") is True
            and row.get("synth_agent_s_start_constructed") is True
            and row.get("synth_scalecua_start_constructed") is True
            and all(
                isinstance(row.get("versions", {}).get(version, {}).get(arm), Mapping)
                and row["versions"][version][arm].get("complete") is True
                for version in ("buggy", "fixed")
                for arm in LAZARUS_ARMS
            )
            and row.get("state_start_constructed") is True
            and row.get("agent_s_start_constructed") is True
            and row.get("p2p_restore_passed") is True
            and row.get("runtime_sha256") == expected_runtime_identities.get(benchmark)
        )
        bindings[task_id]["canary_passed"] = passed
        if not passed:
            failures.append(f"{task_id}: online runtime canary is incomplete")


def _validate_api_preflight(
    value: Mapping[str, Any],
    failures: list[str],
    *,
    expected_provider_profile_sha256: str,
    models: Sequence[str] = EXPECTED_MODELS,
) -> None:
    if value.get("schema") != "c1-lazarus-p2p-only-api-preflight-v1":
        failures.append("P2P-only API preflight schema is invalid")
        return
    if value.get("provider_profile_sha256") != expected_provider_profile_sha256:
        failures.append("P2P-only API preflight provider-profile identity drift")
    rows = value.get("models")
    if not isinstance(rows, list):
        failures.append("P2P-only API preflight model rows are missing")
        return
    index = {str(row.get("model")): row for row in rows if isinstance(row, Mapping)}
    for model in models:
        row = index.get(model)
        if not isinstance(row, Mapping) or row.get("complete") is not True:
            failures.append(f"{model}: API preflight is incomplete")
            continue
        if row.get("thinking_disabled_verified") is not True:
            failures.append(f"{model}: no-thinking API preflight is not verified")
        anomaly = row.get("reasoning_accounting_anomaly")
        if anomaly is not None and not (
            model == "kimi-k2.6@relay"
            and anomaly
            == "provider_reports_one_reasoning_token_without_reasoning_content"
            and row.get("reasoning_tokens") == 1
            and row.get("reasoning_content_present") is False
        ):
            failures.append(f"{model}: unregistered reasoning accounting anomaly")
        from .model_request import ModelRequestProfile, THINKING_DISABLED
        expected = ModelRequestProfile(canonical_model_name(model), THINKING_DISABLED).request_kwargs()
        if row.get("request_overrides") != expected:
            failures.append(f"{model}: no-thinking request override drift")
    fixed_rows = value.get("fixed_routes")
    if not isinstance(fixed_rows, list):
        failures.append("P2P-only fixed observer/judge route probes are missing")
        return
    roles: dict[str, Mapping[str, Any]] = {}
    for row in fixed_rows:
        if not isinstance(row, Mapping):
            continue
        for role in row.get("roles") or ():
            roles[str(role)] = row
    for role in ("observer", "judge"):
        row = roles.get(role)
        if row is None or row.get("complete") is not True:
            failures.append(f"P2P-only {role} route probe is incomplete")


def _optional_artifact(
    workspace: Path, value: str | Path | None, label: str
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    path = _contained(workspace, value, label)
    try:
        return _json_mapping(path, label)
    except ValueError:
        return None


def _key_payload(key: LazarusTrialKey) -> dict[str, Any]:
    return {
        "task_id": key.task_id,
        "model": key.model,
        "seed": key.seed,
        "arm": key.arm,
        "version": key.version,
    }


def _materialize_profile_pair(
    workspace: Path, profile: Any
) -> tuple[Path, Path, Mapping[str, Any], Mapping[str, str]]:
    from .c1_lazarus_selection.p2p_only_gate16 import materialize_pair

    identity = _pair_cache_identity(workspace, profile)
    receipt_path = _pair_receipt_path(workspace, profile.task_id)
    if receipt_path.is_file():
        receipt = _json_mapping(receipt_path, "materialized-pair cache receipt")
        if receipt.get("identity") != identity:
            raise ValueError(
                f"{profile.task_id}: materialized-pair receipt identity drift"
            )
        fixed = _contained(
            workspace, str(receipt.get("fixed_tree") or ""), "fixed tree"
        )
        buggy = _contained(
            workspace, str(receipt.get("buggy_tree") or ""), "buggy tree"
        )
        provenance = receipt.get("version_provenance")
        evidence = receipt.get("materialization")
        if (
            not fixed.is_dir()
            or not buggy.is_dir()
            or not isinstance(provenance, Mapping)
            or any(
                len(str(provenance.get(version) or "")) != 64
                for version in ("fixed", "buggy")
            )
            or not isinstance(evidence, Mapping)
        ):
            raise ValueError(
                f"{profile.task_id}: materialized-pair cache is incomplete"
            )
        bound_evidence = dict(evidence)
        bound_evidence["pair_receipt_sha256"] = _sha256(receipt_path)
        return (
            fixed,
            buggy,
            bound_evidence,
            {
                "fixed": str(provenance["fixed"]),
                "buggy": str(provenance["buggy"]),
            },
        )
    fixed, buggy, evidence = materialize_pair(workspace, profile)
    provenance = {
        "fixed": tree_fingerprint(fixed),
        "buggy": tree_fingerprint(buggy),
    }
    _write_pair_receipt(
        workspace,
        profile,
        fixed=fixed,
        buggy=buggy,
        materialization=evidence,
        provenance=provenance,
    )
    bound_evidence = dict(evidence)
    bound_evidence["pair_receipt_sha256"] = _sha256(receipt_path)
    return fixed, buggy, bound_evidence, provenance


def _version_pair_from_profile(
    *,
    profile: Any,
    fixed: Path,
    buggy: Path,
    pair_evidence: Mapping[str, Any],
    cached_provenance: Mapping[str, str],
) -> LazarusVersionPair:
    build_identity = {
        "track": "c1_lazarus_p2p_only_v1",
        "host": profile.host,
        "materialization": dict(pair_evidence),
        "tree_fingerprint_attestation": {
            "schema": "c1-lazarus-materialized-pair-cache-receipt-v1",
            "task_id": profile.task_id,
            "receipt_sha256": str(pair_evidence.get("pair_receipt_sha256") or ""),
            "version_provenance": dict(cached_provenance),
        },
    }
    fixed_artifact = LazarusVersionArtifact(
        task_id=profile.task_id,
        version="fixed",
        root=fixed.resolve(),
        git_metadata=(fixed / ".git").resolve(),
        source_revision=profile.pinned_revision,
        patch_sha256=profile.inverse_patch_sha256,
        tree_sha256=str(cached_provenance["fixed"]),
        changed_files=(),
        build_identity=build_identity,
        cache_hit=True,
    )
    buggy_artifact = LazarusVersionArtifact(
        task_id=profile.task_id,
        version="buggy",
        root=buggy.resolve(),
        git_metadata=(buggy / ".git").resolve(),
        source_revision=profile.pinned_revision,
        patch_sha256=profile.inverse_patch_sha256,
        tree_sha256=str(cached_provenance["buggy"]),
        changed_files=tuple(profile.production_paths),
        build_identity=build_identity,
        cache_hit=True,
    )
    return LazarusVersionPair(
        buggy=buggy_artifact,
        fixed=fixed_artifact,
        version_switch_ms=0.0,
    )


def _cached_agent_source(
    *,
    workspace: Path,
    task_id: str,
    source_repo: Path,
    source_revision: str,
    forbidden_tokens: Sequence[str],
) -> AgentVisibleSource:
    """Reuse an immutable sanitized source view through a scoped receipt.

    The expensive tree fingerprint and sanitizer scan happen only when the
    cache key is first materialized.  Later readiness runs verify the clean
    source checkout plus a receipt bound to revision, task, sanitizer and
    forbidden-token set; they never rescan unrelated runtime/viewer files.
    """

    normalized_tokens = tuple(sorted({str(value) for value in forbidden_tokens}))
    identity = {
        "schema": "c1-lazarus-agent-source-cache-v1",
        "task_id": task_id,
        "source_revision": source_revision,
        "sanitizer_version": AGENT_SOURCE_SANITIZER_VERSION,
        "materialization_sha256": materialization_sha256(workspace),
        "forbidden_tokens": list(normalized_tokens),
    }
    cache_key = _scoped_canonical_sha256(identity)
    target = (
        workspace
        / ".warptest-cache/lazarus-p2p-only/agent-views-v2"
        / task_id
        / cache_key
    )
    trusted = (
        workspace
        / ".warptest-cache/lazarus-p2p-only/agent-git-v2"
        / task_id
        / f"{cache_key}.git"
    )
    receipt_path = (
        workspace
        / ".warptest-cache/lazarus-p2p-only/agent-view-receipts"
        / task_id
        / f"{cache_key}.json"
    )
    if target.is_dir() and trusted.exists() and receipt_path.is_file():
        receipt = _json_mapping(receipt_path, "agent-source cache receipt")
        sanitized = str(receipt.get("sanitized_tree_sha256") or "")
        if (
            receipt.get("identity") == identity
            and receipt.get("cache_key") == cache_key
            and len(sanitized) == 64
            and not (target / ".git").exists()
        ):
            return AgentVisibleSource(
                task_id=task_id,
                root=target.resolve(),
                trusted_git_dir=trusted.resolve(),
                source_revision=source_revision,
                source_tree_sha256=sanitized,
                sanitized_tree_sha256=sanitized,
                forbidden_tokens=normalized_tokens,
                version_identity_hidden=True,
            )
        raise ValueError(f"{task_id}: agent-source cache receipt drift")
    if target.exists() or trusted.exists() or receipt_path.exists():
        raise ValueError(f"{task_id}: partial agent-source cache entry")
    source = prepare_lazarus_agent_source(
        task_id=task_id,
        source_repo=source_repo,
        source_revision=source_revision,
        destination=target,
        trusted_git_dir=trusted,
        forbidden_tokens=normalized_tokens,
    )
    _atomic_json(
        receipt_path,
        {
            "schema": "c1-lazarus-agent-source-cache-receipt-v1",
            "identity": identity,
            "cache_key": cache_key,
            "sanitized_tree_sha256": source.sanitized_tree_sha256,
        },
    )
    return source


def _pair_cache_identity(workspace: Path, profile: Any) -> dict[str, Any]:
    return {
        "schema": "c1-lazarus-materialized-pair-cache-v1",
        "task_id": profile.task_id,
        "host": profile.host,
        "change_number": profile.change_number,
        "pinned_revision": profile.pinned_revision,
        "inverse_patch_sha256": profile.inverse_patch_sha256,
        "production_paths": list(profile.production_paths),
        "control_test_path": profile.control_test_path,
        "materialization_sha256": materialization_sha256(workspace),
    }


def _pair_receipt_path(workspace: Path, task_id: str) -> Path:
    return (
        workspace
        / ".warptest-cache/lazarus-p2p-only/materialized-pair-receipts"
        / f"{task_id}.json"
    )


def _pair_paths(workspace: Path, profile: Any) -> tuple[Path, Path]:
    trees = workspace / ".warptest-cache/lazarus-p2p-only/trees"
    return (
        trees / f"shared-fixed-{profile.host}-{profile.pinned_revision[:12]}",
        trees / f"{profile.host}-{profile.change_number}-buggy",
    )


def _write_pair_receipt(
    workspace: Path,
    profile: Any,
    *,
    fixed: Path,
    buggy: Path,
    materialization: Mapping[str, Any],
    provenance: Mapping[str, str],
) -> None:
    _atomic_json(
        _pair_receipt_path(workspace, profile.task_id),
        {
            "schema": "c1-lazarus-materialized-pair-cache-receipt-v1",
            "identity": _pair_cache_identity(workspace, profile),
            "fixed_tree": fixed.resolve().relative_to(workspace).as_posix(),
            "buggy_tree": buggy.resolve().relative_to(workspace).as_posix(),
            "version_provenance": dict(provenance),
            "materialization": dict(materialization),
        },
    )


def adopt_materialized_pair_receipts(
    workspace: Path, readiness: Mapping[str, Any]
) -> int:
    """Adopt already-verified pair fingerprints from a complete readiness."""

    if readiness.get("complete") is not True:
        raise ValueError("only a complete readiness can seed pair receipts")
    task_evidence = readiness.get("task_evidence")
    if not isinstance(task_evidence, Mapping):
        raise ValueError("readiness task evidence is missing")
    profiles = {
        profile.task_id: profile
        for profile in load_profile_registry(workspace)
        if profile.catalog_admission
    }
    written = 0
    for task_id, profile in profiles.items():
        evidence = task_evidence.get(task_id)
        provenance = (
            evidence.get("version_provenance")
            if isinstance(evidence, Mapping)
            else None
        )
        if not isinstance(provenance, Mapping) or any(
            len(str(provenance.get(version) or "")) != 64
            for version in ("fixed", "buggy")
        ):
            raise ValueError(f"{task_id}: readiness version provenance is missing")
        fixed, buggy = _pair_paths(workspace, profile)
        if not fixed.is_dir() or not buggy.is_dir():
            raise ValueError(f"{task_id}: materialized pair directory is missing")
        materialization = {
            "fixed_tree": fixed.relative_to(workspace).as_posix(),
            "buggy_tree": buggy.relative_to(workspace).as_posix(),
            "pinned_revision": profile.pinned_revision,
            "fixed_base_revision": profile.pinned_revision,
            "buggy_base_revision": profile.pinned_revision,
            "inverse_changed_paths": sorted(profile.production_paths),
            "production_only_inverse": True,
            "adopted_from_readiness": True,
        }
        receipt = _pair_receipt_path(workspace, task_id)
        if receipt.exists():
            continue
        _write_pair_receipt(
            workspace,
            profile,
            fixed=fixed,
            buggy=buggy,
            materialization=materialization,
            provenance={
                "fixed": str(provenance["fixed"]),
                "buggy": str(provenance["buggy"]),
            },
        )
        written += 1
    return written


def _contained(workspace: Path, value: str | Path, label: str) -> Path:
    candidate = Path(value).expanduser()
    path = (
        candidate.resolve()
        if candidate.is_absolute()
        else (workspace / candidate).resolve()
    )
    if path != workspace and workspace not in path.parents:
        raise ValueError(f"{label} escapes workspace")
    return path


def _yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _git_clean(path: Path) -> bool:
    import subprocess

    result = subprocess.run(
        ("git", "-C", str(path), "status", "--porcelain"),
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return result.returncode == 0 and not result.stdout.strip()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> Path:
    destination = path.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    fd, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return destination


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()


__all__ = [
    "AUTHORIZATION_SCOPE",
    "AUTHORIZATION_SCHEMA",
    "CATALOG_PATH",
    "EXPECTED_CELL_COUNT",
    "EXPECTED_GROUP_COUNT",
    "EXPECTED_MODELS",
    "EXPECTED_TASK_COUNT",
    "INDEX_SCHEMA",
    "MANIFEST_PATH",
    "P2POnlyManifestContract",
    "P2POnlyGroupResult",
    "P2POnlyPlan",
    "P2POnlyReadiness",
    "P2POnlyRun",
    "READINESS_SCHEMA",
    "RUN_SCHEMA",
    "aggregate_p2p_only_reports",
    "adopt_materialized_pair_receipts",
    "build_p2p_only_plan",
    "build_paid_authorization",
    "preflight_p2p_only",
    "run_p2p_only_groups",
    "validate_p2p_only_manifest",
    "write_readiness",
    "write_run_index",
    "write_run_report",
]
