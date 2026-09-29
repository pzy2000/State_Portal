"""Registered manifest contract for the independent C1-Lazarus experiment."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .lazarus_catalog import (
    LAZARUS_ARMS,
    LAZARUS_FORMAL_BENCHMARK_IDS,
    LAZARUS_VERSIONS,
)
from .lazarus_orchestrator import (
    DEFAULT_LAZARUS_MODEL,
    DEFAULT_LAZARUS_SEEDS,
    LAZARUS_ROTATION_RULE,
    PHASE0_DECISION_MODEL,
)
from .lazarus_roster import (
    FORMAL_PAID_RUN_KEY,
    FORMAL_TASK_COUNT,
    FORMAL_TRIAL_COUNT,
    superseded_paid_run_keys,
)
from .model_request import canonical_model_name


_PHASE0_DECISION_MODEL_ID = canonical_model_name(PHASE0_DECISION_MODEL)


@dataclass(frozen=True)
class LazarusManifestContract:
    task_source: str
    task_ids: tuple[str, ...]
    formal_benchmarks: tuple[str, ...]
    arms: tuple[str, ...]
    versions: tuple[str, ...]
    model: str
    seeds: tuple[int, ...]
    observer_model: str
    grounder_model: str
    judge_model: str
    provider_timeout_s: float
    per_trial_p2p_timeout_s: float
    formal_trial_count: int
    phase0_task_ids: tuple[str, str]
    long_chain_task_ids: tuple[str, str, str]
    phase0_paid_pilot_authorized: bool
    formal_paid_run_authorized: bool


@dataclass(frozen=True)
class LazarusPhase0ManifestContract:
    """The explicitly non-formal manifest used before the 24-task catalog exists."""

    task_source: str
    task_ids: tuple[str, str]
    arms: tuple[str, ...]
    versions: tuple[str, ...]
    model: str
    observer_model: str
    grounder_model: str
    judge_model: str
    provider_timeout_s: float
    per_trial_p2p_timeout_s: float
    phase0_trial_count: int
    phase0_paid_pilot_authorized: bool
    formal_paid_run_authorized: bool


def _require_scoped_paid_run(authorization: Mapping[str, Any], *, expected: bool) -> None:
    """Require the paid-run grant that names the current formal scope.

    A grant is scoped to the cell count it was reviewed for.  Honouring an
    older, narrower key would let a past approval cover cells nobody approved,
    so a superseded key is rejected outright rather than treated as equivalent.
    """

    superseded = superseded_paid_run_keys(authorization)
    if superseded:
        raise ValueError(
            f"Lazarus authorization carries superseded paid-run key(s) "
            f"{', '.join(superseded)}; the formal scope is now "
            f"{FORMAL_TRIAL_COUNT} cells and must be re-authorized under "
            f"{FORMAL_PAID_RUN_KEY}"
        )
    if authorization.get(FORMAL_PAID_RUN_KEY) is not expected:
        state = "authorized" if expected else "withheld"
        raise ValueError(
            f"Lazarus {FORMAL_PAID_RUN_KEY} must be {state}"
        )


def validate_lazarus_manifest(
    manifest: Mapping[str, Any],
    *,
    catalog_task_ids: Sequence[str],
    unbound_task_ids: Sequence[str] = (),
) -> LazarusManifestContract:
    """Validate all locked v1 choices before planning or provider access.

    ``unbound_task_ids`` names roster rows whose probe has no runtime binding.
    The caller supplies them because resolving a row's spec needs the repository
    root, which this validator deliberately does not take.
    """

    if manifest.get("schema_version") != 1 or manifest.get("experiment_id") != "c1_lazarus":
        raise ValueError("Lazarus manifest must be c1_lazarus/schema_version 1")
    if manifest.get("task_schema") != "c1_lazarus_v1":
        raise ValueError("Lazarus manifest task_schema must be c1_lazarus_v1")
    task_source = str(manifest.get("tasks") or "").strip()
    if not task_source:
        raise ValueError("Lazarus manifest must register its task source")
    task_ids = tuple(str(value) for value in catalog_task_ids)
    if len(task_ids) != FORMAL_TASK_COUNT or len(task_ids) != len(set(task_ids)):
        raise ValueError(
            f"Lazarus catalog must expose exactly {FORMAL_TASK_COUNT} unique tasks"
        )
    if manifest.get("formal_task_count") != FORMAL_TASK_COUNT:
        raise ValueError(f"Lazarus formal_task_count must be {FORMAL_TASK_COUNT}")
    if tuple(manifest.get("formal_benchmarks") or ()) != LAZARUS_FORMAL_BENCHMARK_IDS:
        raise ValueError(
            "Lazarus formal_benchmarks must be Luanti, JupyterLab, OpenRA"
        )

    arms = manifest.get("arms")
    if not isinstance(arms, Mapping) or tuple(arms) != LAZARUS_ARMS:
        raise ValueError("Lazarus manifest arms must be lazarus_state/lazarus_agent_s")
    expected_methods = {
        "lazarus_state": "state_loop_semantic",
        "lazarus_agent_s": "agent_s_from_start",
    }
    for arm, method in expected_methods.items():
        value = arms.get(arm)
        if not isinstance(value, Mapping) or value.get("method") != method:
            raise ValueError(f"Lazarus arm {arm} must register method {method}")
    if manifest.get("versions") != list(LAZARUS_VERSIONS):
        raise ValueError("Lazarus versions must be ordered buggy, fixed")

    models = manifest.get("models")
    api = manifest.get("api")
    if models != ["gpt-5.6-terra"] or not isinstance(api, Mapping) or api.get("provider") != "relay":
        raise ValueError(f"Lazarus decision model must be {DEFAULT_LAZARUS_MODEL}")
    profiles = manifest.get("model_request_profiles")
    if profiles != {"gpt-5.6-terra": "thinking_disabled"}:
        raise ValueError("Lazarus decision model must use thinking_disabled")
    if manifest.get("observer_model") != "claude-sonnet-5@relay":
        raise ValueError("Lazarus observer model is not registered")
    if manifest.get("grounder_model") != "decision_model":
        raise ValueError("Lazarus grounder must follow the decision model")
    if manifest.get("judge_model") != "qwen3-vl-flash@relay":
        raise ValueError("Lazarus judge model is not registered")
    if manifest.get("provider_timeout_s") != 900:
        raise ValueError("Lazarus provider_timeout_s must be 900")
    if manifest.get("decision_step_budget") != 100:
        raise ValueError("Lazarus decision_step_budget must be 100")
    if manifest.get("budget_ms") != 2_400_000:
        raise ValueError("Lazarus budget_ms must be 2400000")
    if manifest.get("per_trial_p2p_timeout_s") != 300:
        raise ValueError("Lazarus per_trial_p2p_timeout_s must be 300")
    if manifest.get("seeds") != 3:
        raise ValueError("Lazarus manifest must register three seeds")
    formal_count = manifest.get("formal_trial_count")
    if formal_count != FORMAL_TRIAL_COUNT:
        raise ValueError(f"Lazarus formal_trial_count must be {FORMAL_TRIAL_COUNT}")

    scheduler = manifest.get("scheduler")
    expected_scheduler = {
        "rotation_rule": LAZARUS_ROTATION_RULE,
        "ready_scheduler": True,
        "unity_canary": False,
    }
    if scheduler != expected_scheduler:
        raise ValueError("Lazarus scheduler contract does not match v1")
    calibration = manifest.get("calibration")
    if calibration != {"schema": "lazarus-calibration-v1"}:
        raise ValueError("Lazarus calibration must be lazarus-calibration-v1")
    phase0 = manifest.get("phase0")
    if not isinstance(phase0, Mapping):
        raise ValueError("Lazarus Phase 0 registration is required")
    phase0_ids_raw = phase0.get("task_ids")
    if not isinstance(phase0_ids_raw, list):
        raise ValueError("Lazarus Phase 0 task_ids must be a list")
    phase0_ids = tuple(str(value) for value in phase0_ids_raw)
    expected_phase0_shape = {
        "seeds": [0],
        "serial": True,
        "trial_count": 8,
        "stop_on_failure": True,
    }
    if (
        len(phase0_ids) != 2
        or len(set(phase0_ids)) != 2
        or any(value not in task_ids for value in phase0_ids)
        or any(phase0.get(key) != value for key, value in expected_phase0_shape.items())
        or set(phase0) != {"task_ids", *expected_phase0_shape}
    ):
        raise ValueError("Lazarus Phase 0 must register two catalog tasks and eight serial cells")
    long_chain = manifest.get("long_chain")
    if not isinstance(long_chain, Mapping):
        raise ValueError("Lazarus long-chain registration is required")
    long_ids_raw = long_chain.get("task_ids")
    if not isinstance(long_ids_raw, Mapping) or tuple(long_ids_raw) != (
        "luanti",
        "openra",
        "jupyterlab",
    ):
        raise ValueError(
            "Lazarus long-chain must register Luanti, OpenRA, and JupyterLab"
        )
    long_ids = tuple(
        str(long_ids_raw[key]) for key in ("luanti", "openra", "jupyterlab")
    )
    if (
        long_chain.get("excluded_from_formal") is not True
        or long_chain.get("min_k") != 5
        or set(long_chain) != {"excluded_from_formal", "min_k", "task_ids"}
        or len(set(long_ids)) != 3
        or any(value not in task_ids for value in long_ids)
    ):
        raise ValueError("Lazarus long-chain must be excluded from formal with K>=5")
    expected_stats = {
        "bootstrap_iterations": 10_000,
        "ci": 0.95,
        "rng_seed": 1234,
        "mcnemar": "exact",
        "cluster_unit": "task_id",
        "multiple_testing": "holm_h1_h3",
    }
    if manifest.get("stats") != expected_stats:
        raise ValueError("Lazarus statistics must lock exact McNemar, task bootstrap, and Holm H1-H3")
    authorization = manifest.get("authorization")
    if not isinstance(authorization, Mapping):
        raise ValueError("Lazarus manifest requires an authorization block")
    if authorization.get("phase0_paid_pilot") is not True:
        raise ValueError("Lazarus Phase 0 paid pilot must be authorized")
    _require_scoped_paid_run(authorization, expected=True)
    unbound = tuple(str(value) for value in unbound_task_ids)
    if unbound:
        # Paying for a cell that cannot be dispatched would buy a trial that can
        # never produce a verdict.  A row is unbound either because its oracle
        # spec declares no executable probe or because no provider-backed
        # runtime is registered for it.
        raise ValueError(
            "Lazarus formal run cannot be authorized while these rows have no "
            f"executable probe or runtime: {', '.join(sorted(unbound))}"
        )
    return LazarusManifestContract(
        task_source=task_source,
        task_ids=task_ids,
        formal_benchmarks=LAZARUS_FORMAL_BENCHMARK_IDS,
        arms=LAZARUS_ARMS,
        versions=LAZARUS_VERSIONS,
        model=DEFAULT_LAZARUS_MODEL,
        seeds=DEFAULT_LAZARUS_SEEDS,
        observer_model="claude-sonnet-5@relay",
        grounder_model=DEFAULT_LAZARUS_MODEL,
        judge_model="qwen3-vl-flash@relay",
        provider_timeout_s=900.0,
        per_trial_p2p_timeout_s=300.0,
        formal_trial_count=FORMAL_TRIAL_COUNT,
        phase0_task_ids=(phase0_ids[0], phase0_ids[1]),
        long_chain_task_ids=(long_ids[0], long_ids[1], long_ids[2]),
        phase0_paid_pilot_authorized=True,
        formal_paid_run_authorized=True,
    )


def validate_lazarus_phase0_manifest(
    manifest: Mapping[str, Any],
    *,
    catalog_task_ids: Sequence[str],
) -> LazarusPhase0ManifestContract:
    """Validate a two-task Phase-0 manifest without impersonating the formal run.

    The official ``c1_lazarus`` manifest remains impossible to validate until
    all 24 mechanically gated tasks exist.  This separate contract grants only
    the already authorized eight-cell pilot and permanently rejects a formal
    dispatch from the incomplete catalog.
    """

    if (
        manifest.get("schema_version") != 1
        or manifest.get("experiment_id") != "c1_lazarus_phase0"
        or manifest.get("task_schema") != "c1_lazarus_v1"
        or manifest.get("phase0_only") is not True
    ):
        raise ValueError("Lazarus Phase 0 manifest must be c1_lazarus_phase0/schema_version 1")
    task_source = str(manifest.get("tasks") or "").strip()
    if not task_source:
        raise ValueError("Lazarus Phase 0 manifest must register its task source")
    task_ids = tuple(str(value) for value in catalog_task_ids)
    if len(task_ids) != 2 or len(task_ids) != len(set(task_ids)):
        raise ValueError("Lazarus Phase 0 catalog must expose exactly two unique tasks")
    if (
        manifest.get("formal_task_count") != FORMAL_TASK_COUNT
        or manifest.get("formal_trial_count") != FORMAL_TRIAL_COUNT
    ):
        raise ValueError(
            "Lazarus Phase 0 must retain the registered formal design "
            f"{FORMAL_TASK_COUNT}/{FORMAL_TRIAL_COUNT}"
        )

    arms = manifest.get("arms")
    if not isinstance(arms, Mapping) or tuple(arms) != LAZARUS_ARMS:
        raise ValueError("Lazarus Phase 0 arms must be lazarus_state/lazarus_agent_s")
    expected_methods = {
        "lazarus_state": "state_loop_semantic",
        "lazarus_agent_s": "agent_s_from_start",
    }
    if any(
        not isinstance(arms.get(arm), Mapping) or arms[arm].get("method") != method
        for arm, method in expected_methods.items()
    ):
        raise ValueError("Lazarus Phase 0 arm methods drifted")
    if manifest.get("versions") != list(LAZARUS_VERSIONS):
        raise ValueError("Lazarus Phase 0 versions must be ordered buggy, fixed")
    if manifest.get("models") != [_PHASE0_DECISION_MODEL_ID]:
        raise ValueError(f"Lazarus Phase 0 decision model must be {PHASE0_DECISION_MODEL}")
    api = manifest.get("api")
    if not isinstance(api, Mapping) or api.get("provider") != "relay":
        raise ValueError("Lazarus Phase 0 provider must be relay")
    if manifest.get("model_request_profiles") != {
        _PHASE0_DECISION_MODEL_ID: "thinking_disabled"
    }:
        raise ValueError("Lazarus Phase 0 decision model must use thinking_disabled")
    if (
        manifest.get("observer_model") != "claude-sonnet-5@relay"
        or manifest.get("grounder_model") != "decision_model"
        or manifest.get("judge_model") != "qwen3-vl-flash@relay"
    ):
        raise ValueError("Lazarus Phase 0 visual model registration drift")
    if manifest.get("provider_timeout_s") != 900 or manifest.get("decision_step_budget") != 100:
        raise ValueError("Lazarus Phase 0 provider budget drift")
    if manifest.get("budget_ms") != 2_400_000 or manifest.get("per_trial_p2p_timeout_s") != 300:
        raise ValueError("Lazarus Phase 0 runtime budget drift")
    if manifest.get("seeds") != 3:
        raise ValueError("Lazarus Phase 0 must retain formal seed registration")
    expected_scheduler = {
        "rotation_rule": LAZARUS_ROTATION_RULE,
        "ready_scheduler": True,
        "unity_canary": True,
    }
    if manifest.get("scheduler") != expected_scheduler:
        raise ValueError("Lazarus Phase 0 scheduler contract drift")
    if manifest.get("calibration") != {"schema": "lazarus-calibration-v1"}:
        raise ValueError("Lazarus Phase 0 calibration must be lazarus-calibration-v1")
    phase0 = manifest.get("phase0")
    expected_phase0 = {
        "task_ids": list(task_ids),
        "seeds": [0],
        "serial": True,
        "trial_count": 8,
        "stop_on_failure": True,
    }
    if phase0 != expected_phase0:
        raise ValueError("Lazarus Phase 0 must lock the two tasks and eight serial cells")
    if manifest.get("long_chain") != {"status": "deferred_until_formal_catalog"}:
        raise ValueError("Lazarus Phase 0 long-chain diagnostics must remain deferred")
    expected_stats = {
        "bootstrap_iterations": 10_000,
        "ci": 0.95,
        "rng_seed": 1234,
        "mcnemar": "exact",
        "cluster_unit": "task_id",
        "multiple_testing": "holm_h1_h3",
    }
    if manifest.get("stats") != expected_stats:
        raise ValueError("Lazarus Phase 0 statistics registration drift")
    authorization = manifest.get("authorization")
    if (
        not isinstance(authorization, Mapping)
        or authorization.get("phase0_paid_pilot") is not True
    ):
        raise ValueError("Lazarus Phase 0 authorization is invalid")
    _require_scoped_paid_run(authorization, expected=False)
    return LazarusPhase0ManifestContract(
        task_source=task_source,
        task_ids=(task_ids[0], task_ids[1]),
        arms=LAZARUS_ARMS,
        versions=LAZARUS_VERSIONS,
        model=PHASE0_DECISION_MODEL,
        observer_model="claude-sonnet-5@relay",
        grounder_model=PHASE0_DECISION_MODEL,
        judge_model="qwen3-vl-flash@relay",
        provider_timeout_s=900.0,
        per_trial_p2p_timeout_s=300.0,
        phase0_trial_count=8,
        phase0_paid_pilot_authorized=True,
        formal_paid_run_authorized=False,
    )
