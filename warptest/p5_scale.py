"""P5 full-scale experiment orchestrator.

Reads ``benchmarks/p5/manifest.yaml`` and runs the five formal C1 arms (C/D/E/F/G;
GuiAgent A/B temporarily disabled) across the manifest's models and seeds,
stamping every produced report with run provenance (``run_context``) and
recording an auditable ``p5-run-index.json`` that links each (arm, model, seed)
to its report path, status, and retry history.

Execution policy (confirmed for P5):
  * infra / rate-limit failures are retried per the manifest's retry block;
  * exhausted infra failures atomically persist the partial index and pause C1;
  * a model that returns an unusable output is a complete failed trial, not infra.

Within each model×seed block the three arms use the pre-registered cyclic Latin
square. A passed external browser/Luanti calibration artifact gates all paid GUI
calls. C2/C3 remain explicitly selectable legacy arms but are not run by default.

Typical use:
    # plumbing / dry-run
    python3 -m warptest p5-scale --dry-run
    # four-game online shakeout (12 trials = 1 model x 1 seed x 4 tasks x 3 arms)
    python3 -m warptest p5-scale --models glm-5.2 --seeds 1 \
      --task-ids 2048-merge-left,love-office-work-coding,spacerpg-station-save,luanti-set-node \
      --preflight-artifact reports/warptest/c1-preflight.json \
      --calibration-artifact reports/warptest/c1-calibration.json
    # official run; Relay credentials are safely parsed from the manifest's file
    python3 -m warptest p5-scale --preflight-artifact reports/warptest/c1-preflight.json \
      --calibration-artifact reports/warptest/c1-calibration.json --aggregate
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping, Sequence

import yaml

from . import cost, p5_aggregate, run_context
from .c1_experiment import (
    relay_environment,
    calibration_grounder_model,
    grounder_preflight_probes,
    resolve_grounder_model,
)
from .c1_calibration import c1_harness_hash
from .c1_provenance import c1_application_revisions, validate_c1_environment
from .c1_provider_runtime import (
    C1_PROVIDER_TIMEOUT_S,
    manifest_provider_timeout_s,
    provider_timeout_environment,
)
from .c1_recovery_contract import (
    BACKGROUND_RESOURCE_MIGRATION_ID as _BACKGROUND_RESOURCE_MIGRATION_ID,
    JUPYTER_RUNTIME_RECOVERY_STAGE as _JUPYTER_RUNTIME_RECOVERY_STAGE,
    JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE as _JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE,
    SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE,
    TERMINAL_LEDGER_RECOVERY_STAGE as _TERMINAL_LEDGER_RECOVERY_STAGE,
    TrialKey,
    background_recovery_stage,
    interrupted_unknown_usage_snapshot as _interrupted_unknown_usage_snapshot,
)
from .c1_task_deadline import (
    DEFAULT_C1_TASK_TIMEOUT_S,
    validate_task_timeout_s,
)
from .c1_task_resources import c1_task_logical_resources
from .c1_sandbox import C1SandboxManager, preflight_c1_sandboxes
from .c1_unity_canary import (
    UNITY_BENCHMARKS,
    classify_unity_concurrency_canaries,
    run_unity_concurrency_canary,
)
from .c1_concurrency import (
    C1GlobalStop,
    C1ReadyScheduler,
    C1TaskDeferred,
    C1TaskDispatchCancelled,
    build_lane_plan,
    build_ready_task_plan,
)
from .c2_provenance import c2_harness_hash, c2_manifest_fingerprint
from .evo import run_memory_baseline_suite, run_phase3_suite
from .fuzzing import run_fuzz_benchmark
from .gui_agent import GUI_ARM_REPORT_FILE, run_gui_agent_benchmark
from .model_request import (
    ModelRequestProfile,
    PROVIDER_DEFAULT,
    canonical_model_name,
    canonical_profiles_json,
    configured_model_request_profiles,
    model_request_profiles_environment,
)
from .native_input import LUANTI_TEXT_INPUT_PROTOCOL
from .reports import new_report_dir, write_json
from .runtime_lock import (
    DEFAULT_RUNTIME_LOCK_WAIT_TIMEOUT_S,
    RUNTIME_LOCK_DIR_ENV,
    RUNTIME_LOCK_WAIT_TIMEOUT_ENV,
    validate_runtime_lock_wait_timeout_s,
)
from .c1_synthesis_cache import (
    C1_SYNTHESIS_CONSUMER,
    C1_SYNTHESIS_EXPLORE,
    C1_SYNTHESIS_MAX_TOKENS,
)
from .lazarus_manifest import (
    validate_lazarus_manifest,
    validate_lazarus_phase0_manifest,
)
from .lazarus_catalog import LAZARUS_FORMAL_BENCHMARK_IDS
from .lazarus_orchestrator import build_lazarus_trial_plan
from .lazarus_roster import unbound_formal_task_ids
from .lazarus_runtime_support import (
    lazarus_runtime_support_report,
    paid_runtime_unbound_task_ids,
)
from .state_loop import (
    STATE_FUZZ_TASKS,
    load_state_loop_tasks,
    run_state_loop_benchmark,
)
from .c1_source_transition_agent import (
    DeterministicSourceTransitionActionAgent,
)

DEFAULT_MANIFEST = Path("benchmarks/p5/manifest.yaml")
DEFAULT_RUNTIME_LOCK_SCOPE = "machine-shared"
DEFAULT_RUNTIME_LOCK_DIR = Path("/tmp/warptest-runtime-locks")
# Formal C1 arms C/D/E plus the ScaleCUA pixel-baseline extension (F/G).
# GuiAgent A/B are temporarily disabled from the formal set; restore them in
# the manifest and this tuple when re-enabling.  The paper's frozen formal
# comparison subset stays (c1_agent_s, c1_agent_s_synth, c1_state).
C1_ARMS = (
    "c1_agent_s",
    "c1_agent_s_synth",
    "c1_state",
    "c1_agent_scalecua",
    "c1_agent_scalecua_synth",
)
C1_DIAGNOSTIC_ARMS = ("c1_state_minimal_agent",)
C1_EXECUTION_ARMS = C1_ARMS + C1_DIAGNOSTIC_ARMS
# Arms that build their own start by calling the synthesis model. They share one
# checkpoint cache, so an unequal synthesis budget silently splits the cache and
# makes their Start rates incomparable.
C1_SELF_SYNTHESIZING_ARMS = (
    "c1_state",
    "c1_gui_synth",
    "c1_agent_s_synth",
    "c1_agent_scalecua_synth",
)
C1_EXECUTION_SELF_SYNTHESIZING_ARMS = (
    "c1_state",
    "c1_state_minimal_agent",
    "c1_gui_synth",
    "c1_agent_s_synth",
    "c1_agent_scalecua_synth",
)
# The paper's registered formal set is the complete five-arm matrix.
C1_FORMAL_PAPER_ARMS = C1_ARMS
DEFAULT_ARMS = C1_ARMS
# This is intentionally the frozen C1/C2/C3 CLI universe.  Lazarus arms are
# validated only after a Lazarus manifest has selected its separate dispatcher.
ALL_ARMS = C1_EXECUTION_ARMS + ("c2_phase3", "c2_memory_baseline", "c3_fuzz")

# Report filename written by each arm's producer (used to re-stamp retry history).
_ARM_REPORT_FILE = {
    "c1_state": "state-loop.json",
    "c1_state_minimal_agent": "state-minimal-agent.json",
    **GUI_ARM_REPORT_FILE,
    "c2_phase3": "phase3-core-suite.json",
    "c2_memory_baseline": "memory-baseline-suite.json",
    "c3_fuzz": "fuzz.json",
}
_C1_INVOCATION_CONTEXT_FILE = "p5-invocation-context.json"
_C2_INVOCATION_CONTEXT_FILE = "c2-invocation-context.json"


def _recursive_artifact_sha256(root: Path) -> str:
    """Hash a directory as sorted ``file_sha  relative_path`` records."""

    if not root.is_dir():
        raise ValueError(f"C1 interrupted attempt directory is missing: {root}")
    digest = hashlib.sha256()
    files = sorted(
        (path for path in root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(root).as_posix(),
    )
    for path in files:
        relative = path.relative_to(root).as_posix()
        file_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        digest.update(f"{file_sha}  {relative}\n".encode("utf-8"))
    return digest.hexdigest()


def _flatten_persisted_c1_source_for_lineage(
    *, source_path: Path, source: dict[str, Any], source_sha: str
) -> dict[str, Any]:
    """Return the canonical in-memory view of one persisted recovery source.

    Recovery indexes persist ``c1_sources`` rather than the synthetic
    ``flattened_source_lineage`` field used while importing them.  A later
    recovery can therefore encounter another persisted recovery at any lineage
    depth.  Normalize that hop without changing the SHA-bound file on disk so
    recursive evidence resolution treats every generation identically.
    """

    upstream_sources = source.get("c1_sources")
    if not upstream_sources:
        return source
    if (
        not isinstance(upstream_sources, list)
        or len(upstream_sources) != 1
        or not isinstance(upstream_sources[0], dict)
    ):
        raise ValueError("C1 persisted recovery source must bind one upstream source")
    flattened = copy.deepcopy(source)
    flattened.pop("c1_sources", None)
    flattened["flattened_source_lineage"] = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(source_path),
        "derived_from_index_sha256": source_sha,
        "upstream_sources": copy.deepcopy(upstream_sources),
    }
    return flattened


def _validate_jupyter_runtime_recovery_stage(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the one-off recovery from an unbound Jupyter interpreter.

    The interrupted run is itself a recovery source, so this stage deliberately
    stays in the registered background-partition migration family.  That lets
    the importer flatten and recursively re-audit the preceding migration while
    binding this second pause, its unknown-usage attempts, and the exact-usage
    Jupyter harness failure.
    """

    source_index = Path(str(migration.get("source_index") or "")).expanduser()
    source_root = source_index.parent.parent
    pause_path = (
        source_root / "gates/c1-pause-audit-jupyter-runtime.json"
    ).resolve()
    canary_path = (
        source_root / "gates/c1-jupyterlab-runtime-canary.json"
    ).resolve()
    try:
        pause_raw = pause_path.read_bytes()
        pause_audit = json.loads(pause_raw)
        canary_raw = canary_path.read_bytes()
        canary = json.loads(canary_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(
            "C1 Jupyter-runtime recovery audit is unreadable"
        ) from error
    interrupted = (
        pause_audit.get("interrupted_unknown_usage_attempts")
        if isinstance(pause_audit, dict)
        else None
    )
    harness_failures = [
        {
            "arm": "c1_state",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jupyterlab-debug-notebook-rerun",
            "source_status": "error",
        }
    ]
    runtime_python = "/opt/miniconda3/bin/python3.13"
    expected_contract = {
        "migration_stage": _JUPYTER_RUNTIME_RECOVERY_STAGE,
        "runtime_environment_variable": "WARPTEST_JUPYTERLAB_PYTHON",
        "runtime_python": runtime_python,
        "runtime_python_version": "3.13.11",
        "runtime_packages": {
            "jupyterlab": "4.7.0a0",
            "jupyter_server": "2.20.0",
            "jupyter_client": "8.9.1",
        },
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": harness_failures,
        "pause_audit": str(pause_path),
        "pause_audit_sha256": hashlib.sha256(pause_raw).hexdigest(),
        "interrupted_unknown_usage_attempts": interrupted,
        "runtime_canary": str(canary_path),
        "runtime_canary_sha256": hashlib.sha256(canary_raw).hexdigest(),
        "canary_policy": "two_real_no_model_jupyterlab_specs_v1",
    }
    if (
        not isinstance(interrupted, list)
        or len(interrupted) != 4
        or any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        )
    ):
        raise ValueError(
            "C1 Jupyter-runtime recovery migration contract is incomplete"
        )
    configured_python = os.environ.get("WARPTEST_JUPYTERLAB_PYTHON")
    if configured_python is None or Path(configured_python).resolve() != Path(
        runtime_python
    ).resolve():
        raise ValueError(
            "C1 Jupyter-runtime recovery requires the validated Python interpreter"
        )
    checks = canary.get("checks") if isinstance(canary, dict) else None
    if (
        not isinstance(canary, dict)
        or canary.get("passed") is not True
        or canary.get("python_executable") != runtime_python
        or canary.get("python_version") != "3.13.11"
        or canary.get("jupyterlab_version") != "4.7.0a0"
        or canary.get("jupyter_server_version") != "2.20.0"
        or canary.get("jupyter_client_version") != "8.9.1"
        or not isinstance(checks, list)
        or len(checks) != 2
    ):
        raise ValueError("C1 Jupyter-runtime recovery canary did not pass")
    expected_specs = [
        (
            "specs/jupyterlab/text-file-edit.yaml",
            (base / "specs/jupyterlab/text-file-edit.yaml").resolve(),
        ),
        (
            "specs/jupyterlab/c1-debug-notebook-rerun.yaml",
            (base / "specs/jupyterlab/c1-debug-notebook-rerun.yaml").resolve(),
        ),
    ]
    for check, (relative_spec, expected_spec) in zip(
        checks, expected_specs, strict=True
    ):
        if not isinstance(check, dict):
            raise ValueError("C1 Jupyter-runtime recovery canary row is malformed")
        report_path = Path(str(check.get("report") or "")).expanduser()
        recorded_spec = Path(str(check.get("spec") or "")).expanduser()
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
            recorded_spec_raw = recorded_spec.read_bytes()
            expected_spec_raw = expected_spec.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                "C1 Jupyter-runtime recovery canary report is unreadable"
            ) from error
        if (
            check.get("spec_relative_path") != relative_spec
            or check.get("spec_sha256")
            != hashlib.sha256(recorded_spec_raw).hexdigest()
            or recorded_spec_raw != expected_spec_raw
            or check.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or check.get("overall_status") != "success"
            or not isinstance(report, dict)
            or report.get("overall_status") != "success"
            or report.get("semantic_status") != "success"
            or Path(str(report.get("spec_path") or "")).resolve()
            != recorded_spec.resolve()
            or not str(report.get("launch_command") or "").startswith(
                runtime_python + " -m jupyterlab "
            )
        ):
            raise ValueError("C1 Jupyter-runtime recovery canary report is invalid")

    if (
        source.get("run_id") != "p5-b9c6a7b91801"
        or source.get("git_commit")
        != "21f0fb9ab273f7dfaf51efe8354371bb434ade8d"
        or source.get("harness_hash")
        != "b35cb4ef39d93c02d56cbb81f34b3f7279f6b0e70b1ae786a757c55809700201"
    ):
        raise ValueError("C1 Jupyter-runtime recovery source mismatch")
    rows = [
        (arm, row)
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    error_key = TrialKey(
        "c1_state",
        "kimi-k2.6@relay",
        0,
        "jupyterlab-debug-notebook-rerun",
    )
    errors = {
        TrialKey.from_row(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 402
        or sum(row.get("status") == "pending" for _arm, row in rows) != 146
        or errors != {error_key}
    ):
        raise ValueError("C1 Jupyter-runtime recovery coverage mismatch")
    row_by_key = {
        TrialKey.from_row(arm, row): row
        for arm, row in rows
    }
    seen_interrupted: set[TrialKey] = set()
    for item in interrupted:
        if not isinstance(item, dict):
            raise ValueError("C1 Jupyter interrupted-attempt row is malformed")
        key = TrialKey.from_row(str(item.get("arm") or ""), item)
        attempt_dir = Path(str(item.get("attempt_dir") or "")).expanduser()
        files = [path for path in attempt_dir.rglob("*") if path.is_file()]
        if (
            key in seen_interrupted
            or row_by_key.get(key, {}).get("status") != "pending"
            or item.get("trial_attempt") != 0
            or item.get("usage_status") != "unknown"
            or item.get("disposition")
            != "quarantine_primary_attempt_and_register_replacement"
            or item.get("file_count") != len(files)
            or item.get("total_bytes")
            != sum(path.stat().st_size for path in files)
            or item.get("recursive_artifact_sha256")
            != _recursive_artifact_sha256(attempt_dir)
        ):
            raise ValueError("C1 Jupyter interrupted-attempt binding mismatch")
        seen_interrupted.add(key)

    audited_failures = pause_audit.get("registered_exact_usage_harness_failures")
    if not isinstance(audited_failures, list) or len(audited_failures) != 1:
        raise ValueError("C1 Jupyter harness-failure audit is incomplete")
    audited_failure = audited_failures[0]
    error_row = row_by_key[error_key]
    report_path = Path(str(error_row.get("report") or "")).expanduser()
    try:
        report_raw = report_path.read_bytes()
        report = json.loads(report_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("C1 Jupyter harness-failure report is unreadable") from error
    results = report.get("results") if isinstance(report, dict) else None
    result = results[0] if isinstance(results, list) and len(results) == 1 else None
    actual_cost = result.get("actual_cost") if isinstance(result, dict) else None
    if (
        not isinstance(audited_failure, dict)
        or {
            field: audited_failure.get(field)
            for field in ("arm", "model", "seed", "task_id", "source_status")
        }
        != harness_failures[0]
        or audited_failure.get("report") != str(report_path)
        or audited_failure.get("report_sha256")
        != hashlib.sha256(report_raw).hexdigest()
        or error_row.get("report_sha256")
        != hashlib.sha256(report_raw).hexdigest()
        or not isinstance(report, dict)
        or report.get("complete") is not False
        or not isinstance(result, dict)
        or result.get("incomplete") is not True
        or not isinstance(actual_cost, dict)
        or actual_cost.get("usage_complete") is not True
        or any(
            actual_cost.get(field) != 0
            for field in (
                "unknown_prompt_token_calls",
                "unknown_completion_token_calls",
                "unknown_total_token_calls",
            )
        )
        or "JupyterLab runtime dependencies are not installed"
        not in str(result.get("reason") or "")
        or "No module named 'jupyter_server'"
        not in str(result.get("reason") or "")
    ):
        raise ValueError("C1 Jupyter harness-failure binding mismatch")
    if (
        pause_audit.get("paused_index_sha256")
        != migration.get("source_index_sha256")
        or pause_audit.get("run_id") != source.get("run_id")
    ):
        raise ValueError("C1 Jupyter pause audit source binding mismatch")

    source_commit = str(source.get("git_commit") or "")
    current_commit = str(migration.get("current_git_commit") or "")
    completed = __import__("subprocess").run(
        ["git", "diff", "--name-only", source_commit, current_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ValueError("C1 Jupyter-runtime recovery cannot audit the diff")
    changed_paths = sorted(line for line in completed.stdout.splitlines() if line)
    allowed_paths = {
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        set(changed_paths) != allowed_paths
        or migration.get("source_git_commit") != source_commit
        or current_commit != run_context.git_commit(base)
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
    ):
        raise ValueError("C1 Jupyter-runtime recovery diff binding mismatch")


def _validate_jynew_exit_timeout_recovery_stage(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate recovery from a successful Jynew report followed by exit hang."""

    source_index = Path(str(migration.get("source_index") or "")).expanduser()
    source_root = source_index.parent.parent
    pause_path = (
        source_root / "gates/c1-pause-audit-jynew-exit-timeout.json"
    ).resolve()
    canary_path = (
        source_root
        / "gates/c1-jynew-exit-timeout-canary/validated-summary.json"
    ).resolve()
    try:
        pause_raw = pause_path.read_bytes()
        pause_audit = json.loads(pause_raw)
        canary_raw = canary_path.read_bytes()
        canary = json.loads(canary_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("C1 Jynew exit-timeout recovery audit is unreadable") from error

    interrupted = (
        pause_audit.get("interrupted_unknown_usage_attempts")
        if isinstance(pause_audit, dict)
        else None
    )
    harness_failures = [
        {
            "arm": "c1_agent_s_synth",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "jynew-sidequest-trade-submap",
            "source_status": "error",
        },
        {
            "arm": "c1_state",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jynew-sidequest-trade-submap",
            "source_status": "error",
        },
    ]
    expected_contract = {
        "migration_stage": _JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE,
        "lifecycle_policy": "fresh_success_report_after_exit_timeout_v1",
        "stale_report_policy": "delete_before_launch_and_match_request_evidence_v1",
        "visual_timeout_recovery_policy": "require_informative_unity_capture_v1",
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": harness_failures,
        "pause_audit": str(pause_path),
        "pause_audit_sha256": hashlib.sha256(pause_raw).hexdigest(),
        "interrupted_unknown_usage_attempts": interrupted,
        "runtime_canary": str(canary_path),
        "runtime_canary_sha256": hashlib.sha256(canary_raw).hexdigest(),
        "canary_policy": "real_unity_headed_visual_plus_fault_injected_semantic_v1",
    }
    if (
        not isinstance(interrupted, list)
        or len(interrupted) != 8
        or any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        )
    ):
        raise ValueError("C1 Jynew exit-timeout migration contract is incomplete")

    if (
        source.get("run_id") != "p5-2ade80dafbeb"
        or source.get("git_commit")
        != "d89608314b6767cd2a26815a357e2c8aa567be1f"
        or source.get("harness_hash")
        != "b3a22bd498c982aa436bae6ddb2e51da8f4e7f6197a9328484539cbb77db611e"
    ):
        raise ValueError("C1 Jynew exit-timeout recovery source mismatch")
    rows = [
        (arm, row)
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    expected_error_keys = {
        TrialKey.from_row(str(item["arm"]), item)
        for item in harness_failures
    }
    error_keys = {
        TrialKey.from_row(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 448
        or sum(row.get("status") == "pending" for _arm, row in rows) != 99
        or error_keys != expected_error_keys
    ):
        raise ValueError("C1 Jynew exit-timeout recovery coverage mismatch")
    row_by_key = {
        TrialKey.from_row(arm, row): row
        for arm, row in rows
    }

    seen_interrupted: set[TrialKey] = set()
    for item in interrupted:
        if not isinstance(item, dict):
            raise ValueError("C1 Jynew interrupted-attempt row is malformed")
        key = TrialKey.from_row(str(item.get("arm") or ""), item)
        attempt_dir = Path(str(item.get("attempt_dir") or "")).expanduser()
        files = [path for path in attempt_dir.rglob("*") if path.is_file()]
        if (
            key in seen_interrupted
            or row_by_key.get(key, {}).get("status") != "pending"
            or item.get("trial_attempt") != 0
            or item.get("persisted_trial_report") is not None
            or item.get("usage_status") != "unknown"
            or item.get("disposition")
            != "quarantine_primary_attempt_and_register_replacement"
            or item.get("file_count") != len(files)
            or item.get("total_bytes")
            != sum(path.stat().st_size for path in files)
            or item.get("recursive_artifact_sha256")
            != _recursive_artifact_sha256(attempt_dir)
        ):
            raise ValueError("C1 Jynew interrupted-attempt binding mismatch")
        seen_interrupted.add(key)

    audited_failures = pause_audit.get("registered_exact_usage_harness_failures")
    if not isinstance(audited_failures, list) or len(audited_failures) != 2:
        raise ValueError("C1 Jynew harness-failure audit is incomplete")
    audited_by_key = {
        TrialKey.from_row(str(item.get("arm") or ""), item): item
        for item in audited_failures
        if isinstance(item, dict)
    }
    if set(audited_by_key) != expected_error_keys:
        raise ValueError("C1 Jynew harness-failure keys are invalid")
    for expected in harness_failures:
        key = TrialKey.from_row(str(expected["arm"]), expected)
        audited = audited_by_key[key]
        error_row = row_by_key[key]
        report_path = Path(str(error_row.get("report") or "")).expanduser()
        unity_report_path = Path(
            str(audited.get("successful_unity_report_after_timeout") or "")
        ).expanduser()
        screenshot_path = Path(
            str(audited.get("successful_unity_screenshot") or "")
        ).expanduser()
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
            unity_report_raw = unity_report_path.read_bytes()
            unity_report = json.loads(unity_report_raw)
            screenshot_raw = screenshot_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("C1 Jynew harness evidence is unreadable") from error
        results = report.get("results") if isinstance(report, dict) else None
        result = results[0] if isinstance(results, list) and len(results) == 1 else None
        actual_cost = result.get("actual_cost") if isinstance(result, dict) else None
        unity_checks = (
            unity_report.get("checks") if isinstance(unity_report, dict) else None
        )
        if (
            {
                field: audited.get(field)
                for field in ("arm", "model", "seed", "task_id", "source_status")
            }
            != expected
            or audited.get("report") != str(report_path)
            or audited.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or error_row.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or not isinstance(report, dict)
            or report.get("complete") is not False
            or not isinstance(result, dict)
            or result.get("incomplete") is not True
            or not isinstance(actual_cost, dict)
            or actual_cost.get("usage_complete") is not True
            or any(
                actual_cost.get(field) != 0
                for field in (
                    "unknown_prompt_token_calls",
                    "unknown_completion_token_calls",
                    "unknown_reasoning_token_calls",
                    "unknown_total_token_calls",
                )
            )
            or "Unity oracle" not in str(result.get("reason") or "")
            or "timed out after 300.0 seconds" not in str(result.get("reason") or "")
            or audited.get("successful_unity_report_after_timeout_sha256")
            != hashlib.sha256(unity_report_raw).hexdigest()
            or not isinstance(unity_report, dict)
            or unity_report.get("status") != "success"
            or unity_report.get("screenshot_status") != "success"
            or unity_report.get("screenshot_source") != "unity_capture"
            or not isinstance(unity_checks, list)
            or not unity_checks
            or any(
                not isinstance(check, dict) or check.get("status") != "success"
                for check in unity_checks
            )
            or audited.get("successful_unity_report_check_count")
            != len(unity_checks)
            or Path(str(unity_report.get("screenshot_path") or "")).resolve()
            != screenshot_path.resolve()
            or len(screenshot_raw) < 2_000
            or audited.get("successful_unity_screenshot_sha256")
            != hashlib.sha256(screenshot_raw).hexdigest()
        ):
            raise ValueError("C1 Jynew harness-failure binding mismatch")

    if (
        pause_audit.get("paused_index_sha256")
        != migration.get("source_index_sha256")
        or pause_audit.get("run_id") != source.get("run_id")
        or pause_audit.get("tuple_ledger")
        != {"total": 549, "ok": 448, "error": 2, "pending": 99}
        or pause_audit.get("termination", {}).get(
            "remaining_report_root_processes_after_stop"
        )
        != 0
        or pause_audit.get("termination", {}).get(
            "remaining_unity_processes_after_stop"
        )
        != 0
    ):
        raise ValueError("C1 Jynew pause audit source binding mismatch")

    _validate_jynew_exit_timeout_canary(
        base=base,
        canary=canary,
        source_root=source_root,
    )

    source_commit = str(source.get("git_commit") or "")
    current_commit = str(migration.get("current_git_commit") or "")
    completed = __import__("subprocess").run(
        ["git", "diff", "--name-only", source_commit, current_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if completed.returncode != 0:
        raise ValueError("C1 Jynew exit-timeout recovery cannot audit the diff")
    changed_paths = sorted(line for line in completed.stdout.splitlines() if line)
    allowed_paths = {
        "tests/test_c1_trial_persistence.py",
        "tests/test_visual.py",
        "warptest/jynew_runtime.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        set(changed_paths) != allowed_paths
        or migration.get("source_git_commit") != source_commit
        or current_commit != run_context.git_commit(base)
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
    ):
        raise ValueError("C1 Jynew exit-timeout recovery diff binding mismatch")


def _validate_screen_size_probe_cache_recovery_stage(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate recovery from concurrent pre-provider screen-size probes."""

    source_index = Path(str(migration.get("source_index") or "")).expanduser()
    source_root = source_index.parent.parent
    audit_path = (
        source_root / "gates/c1-screen-size-probe-timeout-audit.json"
    ).resolve()
    canary_path = (
        source_root / "gates/c1-screen-size-probe-cache-canary.json"
    ).resolve()
    try:
        audit_raw = audit_path.read_bytes()
        audit = json.loads(audit_raw)
        canary_raw = canary_path.read_bytes()
        canary = json.loads(canary_raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("C1 screen-size probe recovery audit is unreadable") from error

    failure_rows = [
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "luanti-water-source",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "2048-merge-up",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "love-shopping-mall-spa",
            "source_status": "error",
        },
    ]
    expected_contract = {
        "migration_stage": SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE,
        "probe_policy": (
            "process_wide_single_flight_cached_backing_store_measurement_v1"
        ),
        "failure_usage_policy": (
            "pre_provider_exact_zero_retryable_infrastructure_v1"
        ),
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "preserved_model_failure_trials": [],
        "registered_harness_failure_trials": failure_rows,
        "pause_audit": str(audit_path),
        "pause_audit_sha256": hashlib.sha256(audit_raw).hexdigest(),
        "interrupted_unknown_usage_attempts": [],
        "runtime_canary": str(canary_path),
        "runtime_canary_sha256": hashlib.sha256(canary_raw).hexdigest(),
        "canary_policy": "real_screencapture_32_thread_single_flight_v1",
    }
    if any(
        migration.get(field) != value
        for field, value in expected_contract.items()
    ):
        raise ValueError("C1 screen-size probe migration contract is incomplete")

    rows = [
        (arm, row)
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    error_keys = {
        TrialKey.from_row(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    expected_error_keys = {
        TrialKey.from_row(str(item["arm"]), item)
        for item in failure_rows
    }
    if (
        source.get("run_id") != "p5-ecb702f63cf4"
        or source.get("git_commit")
        != "d18542757cdf178976eb33922c407ab2745dc813"
        or source.get("harness_hash")
        != "59b63b0407cf85414d930bcdfb25ba0c62442abce4a188a226c83e4e57b22237"
        or len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 480
        or sum(row.get("status") == "pending" for _arm, row in rows) != 64
        or sum(
            row.get("status") == "quarantined_unknown_usage"
            for _arm, row in rows
        )
        != 2
        or error_keys != expected_error_keys
    ):
        raise ValueError("C1 screen-size probe recovery source mismatch")

    log_path = Path(str(audit.get("run_log") or "")).expanduser()
    try:
        log_raw = log_path.read_bytes()
    except OSError as error:
        raise ValueError("C1 screen-size timeout log is unreadable") from error
    if (
        audit.get("schema_version") != 1
        or audit.get("kind") != "c1_screen_size_probe_timeout_audit"
        or audit.get("run_id") != source.get("run_id")
        or audit.get("source_index") != str(source_index)
        or audit.get("source_index_sha256")
        != migration.get("source_index_sha256")
        or audit.get("tuple_ledger")
        != {
            "total": 549,
            "ok": 480,
            "pending": 64,
            "error": 3,
            "quarantined_unknown_usage": 2,
        }
        or audit.get("interrupted_unknown_usage_attempts") != []
        or audit.get("failure")
        != {
            "phase": "pre_policy_screen_size_probe",
            "exception_type": "subprocess.TimeoutExpired",
            "command": "screencapture -x -t png <temporary>/probe.png",
            "timeout_seconds": 15,
            "provider_calls_before_failure": 0,
            "usage_disposition": "exact_zero_retryable_infrastructure",
            "process_exit_was_autonomous": True,
        }
        or audit.get("run_log_sha256")
        != hashlib.sha256(log_raw).hexdigest()
        or b"screencapture" not in log_raw
        or b"timed out after 15 seconds" not in log_raw
    ):
        raise ValueError("C1 screen-size timeout audit binding mismatch")

    audited_attempts = audit.get("registered_attempts")
    audited_by_key = {
        TrialKey.from_row(str(item.get("arm") or ""), item): item
        for item in (audited_attempts or [])
        if isinstance(item, dict)
    }
    row_by_key = {
        TrialKey.from_row(arm, row): row
        for arm, row in rows
    }
    if set(audited_by_key) != expected_error_keys:
        raise ValueError("C1 screen-size registered-attempt keys are incomplete")
    for key in expected_error_keys:
        audited = audited_by_key[key]
        row = row_by_key[key]
        report_path = Path(str(row.get("report") or "")).expanduser()
        sidecar_path = report_path.parent / _C1_INVOCATION_CONTEXT_FILE
        try:
            report_raw = report_path.read_bytes()
            report = json.loads(report_raw)
            sidecar_raw = sidecar_path.read_bytes()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                "C1 screen-size registered-attempt evidence is unreadable"
            ) from error
        results = report.get("results") if isinstance(report, dict) else None
        result = (
            results[0]
            if isinstance(results, list)
            and len(results) == 1
            and isinstance(results[0], dict)
            else None
        )
        if (
            not isinstance(result, dict)
            or audited.get("trial_attempt") != row.get("trial_attempt")
            or audited.get("report") != str(report_path)
            or audited.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or row.get("report_sha256")
            != hashlib.sha256(report_raw).hexdigest()
            or audited.get("sidecar_sha256")
            != hashlib.sha256(sidecar_raw).hexdigest()
            or P5Orchestrator._registered_source_error_kind(
                result=result,
                report=report,
                source=source,
                task_id=key[3],
            )
            != "exact_usage_retryable_infrastructure"
            or result.get("reason")
            != "pre-policy macOS screen-size probe timed out before any provider call"
            or int(result.get("actual_cost", {}).get("llm_calls", -1)) != 0
        ):
            raise ValueError("C1 screen-size registered-attempt binding mismatch")

    if (
        not isinstance(canary, dict)
        or canary.get("passed") is not True
        or canary.get("policy")
        != "real_screencapture_32_thread_single_flight_v1"
        or canary.get("thread_count") != 32
        or canary.get("repetitions_per_thread") != 8
        or canary.get("physical_capture_count") != 1
        or canary.get("unique_dimensions") != [[3024, 1964]]
        or canary.get("errors") != []
        or canary.get("git_commit") != migration.get("current_git_commit")
        or canary.get("harness_hash") != migration.get("current_harness_hash")
    ):
        raise ValueError("C1 screen-size single-flight canary did not pass")

    source_commit = str(source.get("git_commit") or "")
    current_commit = str(migration.get("current_git_commit") or "")
    completed = __import__("subprocess").run(
        ["git", "diff", "--name-only", source_commit, current_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    changed_paths = sorted(line for line in completed.stdout.splitlines() if line)
    required_paths = {
        "tests/test_c1_trial_persistence.py",
        "tests/test_native_window.py",
        "warptest/native_window.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        completed.returncode != 0
        or set(changed_paths) != required_paths
        or migration.get("source_git_commit") != source_commit
        or current_commit != run_context.git_commit(base)
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
    ):
        raise ValueError("C1 screen-size probe recovery diff binding mismatch")


def _validate_jynew_exit_timeout_canary(
    *, base: Path, canary: dict[str, Any], source_root: Path
) -> None:
    """Re-open every real Unity artifact named by the canary suite."""

    canary_root = (
        source_root / "gates/c1-jynew-exit-timeout-canary"
    ).resolve()
    expected_paths = {
        "headed_visual_canary": canary_root / "headed-visual-summary.json",
        "fault_injected_semantic_canary": canary_root
        / "fault-injected-semantic-summary.json",
        "invalid_visual_rejection_canary": canary_root
        / "fault-injected-summary.json",
        "fault_wrapper": canary_root / "unity-success-then-hold.sh",
    }
    loaded: dict[str, tuple[bytes, Any]] = {}
    try:
        for field, expected_path in expected_paths.items():
            recorded = Path(str(canary.get(field) or "")).expanduser().resolve()
            if recorded != expected_path:
                raise ValueError(f"unexpected Jynew canary path: {field}")
            raw = recorded.read_bytes()
            value = raw if field == "fault_wrapper" else json.loads(raw)
            loaded[field] = (raw, value)
            if canary.get(field + "_sha256") != hashlib.sha256(raw).hexdigest():
                raise ValueError(f"Jynew canary SHA mismatch: {field}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("C1 Jynew canary artifact is unreadable") from error

    visual = loaded["headed_visual_canary"][1]
    semantic = loaded["fault_injected_semantic_canary"][1]
    negative = loaded["invalid_visual_rejection_canary"][1]
    spec_path = (base / "specs/jynew/c1-sidequest-trade-submap.yaml").resolve()
    expected_spec_sha = hashlib.sha256(spec_path.read_bytes()).hexdigest()

    def bound_file(payload: dict[str, Any], path_value: Any, sha_field: str) -> bool:
        path = Path(str(path_value or "")).expanduser()
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        return payload.get(sha_field) == hashlib.sha256(raw).hexdigest()

    visual_result = visual.get("result") if isinstance(visual, dict) else None
    visual_utility = (
        visual_result.get("utility_report")
        if isinstance(visual_result, dict)
        else None
    )
    visual_checks = (
        visual_result.get("checks") if isinstance(visual_result, dict) else None
    )
    if (
        not isinstance(visual, dict)
        or visual.get("passed") is not True
        or visual.get("headless") is not False
        or visual.get("visual_capture") is not True
        or visual.get("source_unchanged") is not True
        or visual.get("sandbox_cleaned") is not True
        or visual.get("spec_sha256") != expected_spec_sha
        or not isinstance(visual_result, dict)
        or visual_result.get("status") != "success"
        or visual_result.get("live_source") != "unity_capture"
        or visual_result.get("screenshot_status") != "success"
        or visual_result.get("screenshot_source") != "unity_capture"
        or not isinstance(visual_utility, dict)
        or visual_utility.get("evidence_task_id")
        != "jynew-sidequest-trade-submap"
        or visual_utility.get("evidence_stage") != "canary"
        or not isinstance(visual_checks, list)
        or not visual_checks
        or any(check.get("status") != "success" for check in visual_checks)
        or not bound_file(
            visual,
            visual_result.get("report_path"),
            "result_report_sha256",
        )
        or not bound_file(
            visual,
            visual_utility.get("screenshot_path"),
            "screenshot_sha256",
        )
    ):
        raise ValueError("C1 Jynew headed visual canary did not pass")

    semantic_result = (
        semantic.get("result") if isinstance(semantic, dict) else None
    )
    semantic_utility = (
        semantic_result.get("utility_report")
        if isinstance(semantic_result, dict)
        else None
    )
    semantic_checks = (
        semantic_result.get("checks") if isinstance(semantic_result, dict) else None
    )
    if (
        not isinstance(semantic, dict)
        or semantic.get("passed") is not True
        or semantic.get("fault_injection")
        != "real_unity_success_then_launcher_hold_v1"
        or semantic.get("source_unchanged") is not True
        or semantic.get("sandbox_cleaned") is not True
        or semantic.get("spec_sha256") != expected_spec_sha
        or not isinstance(semantic_result, dict)
        or semantic_result.get("status") != "success"
        or semantic_result.get("process_exit_status")
        != "timed_out_after_success_report"
        or semantic_result.get("report_recovered_after_exit_timeout") is not True
        or not isinstance(semantic_utility, dict)
        or semantic_utility.get("status") != "success"
        or semantic_utility.get("evidence_task_id")
        != "jynew-sidequest-trade-submap"
        or semantic_utility.get("evidence_stage") != "canary"
        or not isinstance(semantic_checks, list)
        or len(semantic_checks) != 16
        or any(check.get("status") != "success" for check in semantic_checks)
        or not bound_file(
            semantic,
            semantic_result.get("report_path"),
            "result_report_sha256",
        )
    ):
        raise ValueError("C1 Jynew exit-timeout semantic canary did not pass")

    negative_result = negative.get("result") if isinstance(negative, dict) else None
    negative_detail = (
        negative_result.get("utility_report", {}).get("detail")
        if isinstance(negative_result, dict)
        else ""
    )
    expected_checks = canary.get("checks")
    if (
        not isinstance(negative, dict)
        or negative.get("passed") is not False
        or negative.get("source_unchanged") is not True
        or negative.get("sandbox_cleaned") is not True
        or not isinstance(negative_result, dict)
        or negative_result.get("status") != "failure"
        or negative_result.get("process_exit_status")
        != "timed_out_without_valid_success_report"
        or "informative Unity capture" not in str(negative_detail or "")
        or canary.get("policy")
        != "real_unity_headed_visual_plus_fault_injected_semantic_v1"
        or canary.get("passed") is not True
        or not isinstance(expected_checks, dict)
        or not expected_checks
        or not all(value is True for value in expected_checks.values())
    ):
        raise ValueError("C1 Jynew invalid-visual rejection canary did not pass")


def _validate_source_action_sidecar_migration(
    *, base: Path, source: dict[str, Any], migration: dict[str, Any]
) -> None:
    """Bind the E-prime sidecar-reader repair to its exact stopped source.

    Only provenance readers change. Original reports, prompts, checkpoints,
    failed attempts, and recursive source indexes remain immutable.
    """
    source_commit = "2a5f0432fe16343bedb108db736c5c1fa6ae0dc7"
    source_hash = "b226fd303fa779ed72741b623fd6d14b72c0b20a6bf2e8268174c7ea7f7e0d83"
    source_sha = "466f7a05c4f8c1eb828f58ef1f87dfeec75a6b49bd6e1336f3580b1b1f086d00"
    source_path = Path(str(migration.get("source_index") or "")).resolve()
    if (
        source.get("run_id") != "p5-425756d8841e"
        or source.get("git_commit") != source_commit
        or source.get("harness_hash") != source_hash
        or migration.get("source_index_sha256") != source_sha
        or hashlib.sha256(source_path.read_bytes()).hexdigest() != source_sha
    ):
        raise ValueError("C1 source-action sidecar migration source mismatch")
    persisted = json.loads(source_path.read_text(encoding="utf-8"))
    if source != _flatten_persisted_c1_source_for_lineage(
        source_path=source_path, source=persisted, source_sha=source_sha
    ):
        raise ValueError("C1 source-action sidecar migration lineage mismatch")
    preserved = [
        {"arm": arm, "model": model, "seed": seed, "task_id": task_id,
         "source_status": "error"}
        for arm, model, seed, task_id in (
            ("c1_agent_s_synth", "kimi-k2.6@relay", 2, "openra-disable-edge-scroll"),
            ("c1_agent_s_synth", "kimi-k2.6@relay", 2, "jupyterlab-reproducible-experiment-workspace"),
            ("c1_state", "gemini-3.5-flash@relay", 1, "openra-allied-combined-arms-rally"),
        )
    ]
    rows = [(arm, row) for arm, entries in source.get("arms", {}).items()
            for row in entries]
    preserved_keys = {TrialKey.from_row(row["arm"], row) for row in preserved}
    affected = [
        {"arm": arm, "model": row.get("model"), "seed": row.get("seed"),
         "task_id": row.get("task_id"), "source_status": row.get("status")}
        for arm, row in rows
        if row.get("status") != "ok" and TrialKey.from_row(arm, row) not in preserved_keys
    ]
    affected.sort(key=lambda row: (row["arm"], row["model"], row["seed"], row["task_id"]))
    expected = {
        "migration_id": "c1_source_action_sidecar_validation_v1",
        "source_run_id": "p5-425756d8841e",
        "source_git_commit": source_commit,
        "source_harness_hash": source_hash,
        "affected_trials": affected,
        "preserved_model_failure_trials": [],
        "preserved_operational_failure_trials": preserved,
        "registered_harness_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
        "sidecar_reader_policy": "validate_source_action_prompt_revision_v1",
        "consumed_recovery_attempts": [{
            "arm": "c1_state_minimal_agent", "model": "gemini-3.5-flash@relay",
            "seed": 1, "task_id": "openra-allied-combined-arms-rally", "attempt_count": 1,
        }],
    }
    if any(migration.get(key) != value for key, value in expected.items()):
        raise ValueError("C1 source-action sidecar migration contract mismatch")
    counts = {status: sum(row.get("status") == status for _, row in rows)
              for status in ("ok", "error", "pending")}
    if len(rows) != 2196 or counts != {"ok": 1931, "error": 4, "pending": 261}:
        raise ValueError("C1 source-action sidecar migration coverage mismatch")
    for arm, row in rows:
        if TrialKey.from_row(arm, row) not in preserved_keys:
            continue
        attempts = {
            item.get("trial_attempt") for item in source.get("c1_operational_overhead", [])
            if TrialKey.from_row(str(item.get("arm")), item) == TrialKey.from_row(arm, row)
            and item.get("source_provenance") is None
        }
        if row.get("status") != "error" or row.get("trial_attempt") != 2 or attempts != {0, 1, 2}:
            raise ValueError("C1 source-action sidecar migration exhausted ledger mismatch")
    if len(affected) != 262 or any(row["arm"] != "c1_state_minimal_agent" for row in affected):
        raise ValueError("C1 source-action sidecar migration requeues completed work")

    # Reconstruct the allowed reader-only patch from the original committed code.
    # The migration auditor itself is not execution code and is removed below.
    ast = __import__("ast")
    subprocess = __import__("subprocess")
    result = subprocess.run(["git", "show", f"{source_commit}:warptest/p5_scale.py"],
                            cwd=base, capture_output=True, text=True, check=True)
    old_tree = ast.parse(result.stdout)
    new_tree = ast.parse((base / "warptest/p5_scale.py").read_text(encoding="utf-8"))
    new_tree.body = [node for node in new_tree.body if not (
        isinstance(node, ast.FunctionDef) and node.name == "_validate_source_action_sidecar_migration"
    )]
    old_class = next(node for node in old_tree.body if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator")
    methods = {node.name: node for node in old_class.body if isinstance(node, ast.FunctionDef)}
    validator = methods["_validate_existing_replacement_row"]
    additions = 0
    for node in ast.walk(validator):
        if isinstance(node, ast.Dict) and any(isinstance(key, ast.Constant) and key.value == "source_action_format_repairs" for key in node.keys):
            node.keys.append(ast.Constant("source_action_prompt_revision"))
            node.values.append(ast.parse('str(cfg.get("source_action_prompt_revision") or "")', mode="eval").body)
            additions += 1
    completed = methods["_completed"]
    for node in ast.walk(completed):
        if isinstance(node, ast.Set) and any(isinstance(value, ast.Constant) and value.value == "exact_usage_nonretryable_model_failure" for value in node.elts):
            node.elts.append(ast.Constant("exact_usage_exhausted_retryable_infrastructure"))
            additions += 1
    for node in ast.walk(methods["_import_c1_source_trials"]):
        if isinstance(node, ast.Set) and any(isinstance(value, ast.Constant) and value.value == "c1_atomic_defer_sidecar_cleanup_v1" for value in node.elts):
            node.elts.append(ast.Constant("c1_source_action_sidecar_validation_v1"))
            additions += 1
    recovery_selector = methods["_recovery_trial_attempt"]
    for position, node in enumerate(recovery_selector.body):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "maximum" for target in node.targets):
            recovery_selector.body[position + 1:position + 1] = ast.parse('''migration = getattr(self, "c1_source_contract_migration_metadata", None) or {}
if migration.get("migration_id") == "c1_source_action_sidecar_validation_v1":
    for consumed in migration["consumed_recovery_attempts"]:
        if (consumed["arm"], consumed["model"], consumed["seed"], consumed["task_id"]) == (arm, model, seed, task_id):
            maximum -= consumed["attempt_count"]
''').body
            additions += 1
            break
    dispatcher = next(node for node in old_tree.body if isinstance(node, ast.FunctionDef) and node.name == "_validate_registered_c1_harness_migration")
    dispatcher.body.insert(1, ast.parse('''if migration.get("migration_id") == "c1_source_action_sidecar_validation_v1":
    _validate_source_action_sidecar_migration(base=base, source=source, migration=migration)
    return
''').body[0])
    current_class = next(
        node for node in new_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator"
    )
    for position, node in enumerate(current_class.body):
        if not isinstance(node, ast.FunctionDef):
            continue
        rendered = ast.unparse(node)
        if node.name == "_completed":
            if "exact_usage_exhausted_operational_failure" not in rendered:
                raise ValueError("C1 operational terminal completion validation is missing")
            current_class.body[position] = methods[node.name]
        elif node.name == "_import_c1_source_trials":
            required = (
                "preserved_operational_failure_trials",
                "exact_usage_exhausted_operational_failure",
                "imported_terminal_operational_failure_trials",
            )
            if any(value not in rendered for value in required):
                raise ValueError("C1 operational terminal import validation is incomplete")
            current_class.body[position] = methods[node.name]
    if additions != 4 or ast.dump(old_tree) != ast.dump(new_tree):
        raise ValueError("C1 source-action sidecar migration changed unregistered execution code")
    old_aggregate = ast.parse(subprocess.run(
        ["git", "show", f"{source_commit}:warptest/p5_aggregate.py"],
        cwd=base, capture_output=True, text=True, check=True,
    ).stdout)
    new_aggregate = ast.parse((base / "warptest/p5_aggregate.py").read_text(encoding="utf-8"))
    new_aggregate.body = [node for node in new_aggregate.body if not (
        isinstance(node, ast.FunctionDef) and node.name == "_registered_source_action_sidecar_migration"
    )]
    aggregate_changes = 0
    old_aggregate_functions = {
        node.name: node for node in old_aggregate.body if isinstance(node, ast.FunctionDef)
    }
    for position, function in enumerate(new_aggregate.body):
        if not isinstance(function, ast.FunctionDef):
            continue
        if function.name == "_c1_trial_recovery_issues":
            rendered = ast.unparse(function)
            if (
                "c1_source_action_sidecar_validation_v1" not in rendered
                or "preserved_operational_failure_trials" not in rendered
            ):
                raise ValueError("C1 sidecar aggregate recovery summary is incomplete")
            new_aggregate.body[position] = old_aggregate_functions[function.name]
            aggregate_changes += 1
        elif function.name == "_c1_flattened_trial_recovery_issues":
            rendered = ast.unparse(function)
            if (
                "_registered_source_action_sidecar_migration" not in rendered
                or "exact_usage_exhausted_operational_failure" not in rendered
            ):
                raise ValueError("C1 sidecar flattened-source audit is incomplete")
            new_aggregate.body[position] = old_aggregate_functions[function.name]
            aggregate_changes += 1
    if aggregate_changes != 2 or ast.dump(old_aggregate) != ast.dump(new_aggregate):
        raise ValueError("C1 source-action sidecar migration changed unregistered aggregation code")
    changed = subprocess.run(["git", "diff", "--name-only", source_commit, "--"],
                             cwd=base, capture_output=True, text=True, check=True)
    changed_paths = sorted(changed.stdout.splitlines())
    expected_paths = ["tests/test_c1_trial_persistence.py", "warptest/p5_aggregate.py", "warptest/p5_scale.py"]
    if changed_paths != expected_paths or migration.get("changed_paths") != changed_paths:
        raise ValueError("C1 source-action sidecar migration changed paths mismatch")
    if migration.get("changed_paths_sha256") != hashlib.sha256(json.dumps(changed_paths, separators=(",", ":")).encode()).hexdigest():
        raise ValueError("C1 source-action sidecar migration diff hash mismatch")
    untracked = subprocess.run(["git", "ls-files", "--others", "--exclude-standard", "--", "warptest"],
                               cwd=base, capture_output=True, text=True, check=True)
    if untracked.stdout.strip() or c1_harness_hash(base) != migration.get("current_harness_hash"):
        raise ValueError("C1 source-action sidecar migration current harness mismatch")


def _validate_registered_c1_harness_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate a treatment-neutral migration before crossing a harness hash."""

    if migration.get("migration_id") == "c1_source_action_sidecar_validation_v1":
        _validate_source_action_sidecar_migration(
            base=base, source=source, migration=migration
        )
        return

    migration_stage = background_recovery_stage(migration)
    if migration_stage == _JUPYTER_RUNTIME_RECOVERY_STAGE:
        _validate_jupyter_runtime_recovery_stage(
            base=base,
            source=source,
            migration=migration,
        )
        return

    if migration_stage == _JYNEW_EXIT_TIMEOUT_RECOVERY_STAGE:
        _validate_jynew_exit_timeout_recovery_stage(
            base=base,
            source=source,
            migration=migration,
        )
        return

    if migration_stage == SCREEN_SIZE_PROBE_CACHE_RECOVERY_STAGE:
        _validate_screen_size_probe_cache_recovery_stage(
            base=base,
            source=source,
            migration=migration,
        )
        return

    if migration_stage == _TERMINAL_LEDGER_RECOVERY_STAGE:
        preserved = [
            {
                "arm": "c1_agent_s",
                "model": "kimi-k2.6@relay",
                "seed": 0,
                "task_id": "pal3-story-party-scene-roundtrip",
                "source_status": "error",
            }
        ]
        expected_contract = {
            "terminal_ledger_policy": "nonretryable_report_is_terminal_row_v1",
            "affected_trials": [],
            "preserved_model_failure_trials": preserved,
            "registered_harness_failure_trials": [],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "recursive_lineage_preserved": True,
            "treatment_semantics_unchanged": True,
        }
        if any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError("C1 terminal-ledger migration contract is incomplete")
        if (
            source.get("run_id") != "p5-1a85a3d52fc4"
            or source.get("git_commit")
            != "bc7ef6f90f0148a6ed7a44a53e65068d1de80338"
            or source.get("harness_hash")
            != "009890a3f956929258ef9fc101b12fde15dea6bef31b41bbe904eb1627535d17"
        ):
            raise ValueError("C1 terminal-ledger migration source mismatch")
        rows = [
            (arm, row)
            for arm in C1_ARMS
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict)
        ]
        errors = {
            TrialKey.from_row(arm, row)
            for arm, row in rows
            if row.get("status") == "error"
        }
        expected_error = {
            TrialKey(
                "c1_agent_s",
                "kimi-k2.6@relay",
                0,
                "pal3-story-party-scene-roundtrip",
            )
        }
        if (
            len(rows) != 549
            or sum(row.get("status") == "ok" for _arm, row in rows) != 536
            or sum(row.get("status") == "pending" for _arm, row in rows) != 12
            or errors != expected_error
        ):
            raise ValueError("C1 terminal-ledger migration coverage mismatch")
        source_commit = str(source.get("git_commit") or "")
        current_commit = run_context.git_commit(base)
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, current_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("C1 terminal-ledger migration cannot audit the diff")
        changed_paths = sorted(line for line in completed.stdout.splitlines() if line)
        expected_paths = [
            "tests/test_c1_trial_persistence.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
        ]
        if (
            changed_paths != expected_paths
            or migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit") != current_commit
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256")
            != hashlib.sha256(
                json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
        ):
            raise ValueError("C1 terminal-ledger migration diff binding mismatch")
        return

    if migration.get("migration_id") == _BACKGROUND_RESOURCE_MIGRATION_ID:
        preserved_failures = [
            {
                "arm": "c1_agent_s_synth",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "jynew-submap-save-restore",
                "source_status": "error",
            },
            {
                "arm": "c1_state",
                "model": "kimi-k2.6@relay",
                "seed": 0,
                "task_id": "2048-merge-down",
                "source_status": "error",
            },
        ]
        pause_path = Path(str(migration.get("pause_audit") or "")).expanduser()
        try:
            pause_raw = pause_path.read_bytes()
            pause_audit = json.loads(pause_raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("C1 background-partition pause audit is unreadable") from error
        interrupted = (
            pause_audit.get("interrupted_unknown_usage_attempts")
            if isinstance(pause_audit, dict)
            else None
        )
        canary_root = (
            base
            / "reports/warptest/c1-background-resource-partition-canary-20260807"
        ).resolve()
        background_canary_path = canary_root / "background-final/summary.json"
        unity_canary_path = canary_root / "unity-final/summary.json"
        try:
            background_canary_raw = background_canary_path.read_bytes()
            background_canary = json.loads(background_canary_raw)
            unity_canary_raw = unity_canary_path.read_bytes()
            unity_canary = json.loads(unity_canary_raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError(
                "C1 background-partition concurrency canary is unreadable"
            ) from error
        expected_contract = {
            "resource_policy": "background_native_partition_v1",
            "scheduler_policy": "global_resource_ready_round_robin_v2",
            "scheduler_lease_binding": "c1_task_resource_config_v1",
            "native_input_compatibility": "explicit_foreground_required_v1",
            "unity_serialization_policy": "per_benchmark_canary_fallback_v1",
            "serialized_unity_benchmarks": ["jynew"],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "recursive_lineage_preserved": True,
            "treatment_semantics_unchanged": True,
            "preserved_model_failure_trials": preserved_failures,
            "registered_harness_failure_trials": [],
            "pause_audit": str(pause_path),
            "pause_audit_sha256": hashlib.sha256(pause_raw).hexdigest(),
            "interrupted_unknown_usage_attempts": interrupted,
            "background_concurrency_canary": str(background_canary_path),
            "background_concurrency_canary_sha256": hashlib.sha256(
                background_canary_raw
            ).hexdigest(),
            "unity_concurrency_canary": str(unity_canary_path),
            "unity_concurrency_canary_sha256": hashlib.sha256(
                unity_canary_raw
            ).hexdigest(),
            "canary_policy": "no_provider_all_background_backends_v1",
        }
        if (
            not isinstance(interrupted, list)
            or len(interrupted) != 5
            or any(
                migration.get(field) != value
                for field, value in expected_contract.items()
            )
        ):
            raise ValueError(
                "C1 background-partition migration contract is incomplete"
            )
        background_results = (
            background_canary.get("results")
            if isinstance(background_canary, dict)
            else None
        )
        unity_rows = (
            unity_canary.get("benchmarks")
            if isinstance(unity_canary, dict)
            else None
        )
        if (
            not isinstance(background_canary, dict)
            or background_canary.get("passed") is not True
            or background_canary.get("benchmarks") != ["luanti", "openra"]
            or not isinstance(background_results, list)
            or len(background_results) != 2
            or any(
                row.get("passed") is not True
                or row.get("overlap_ns", 0) <= 0
                or row.get("distinct_pids") is not True
                or row.get("distinct_sessions") is not True
                or row.get("distinct_support_dirs") is not True
                or row.get("distinct_report_dirs") is not True
                or row.get("source_unchanged") is not True
                or row.get("sandboxes_cleaned") is not True
                for row in background_results
                if isinstance(row, dict)
            )
            or not all(isinstance(row, dict) for row in background_results)
            or not isinstance(unity_canary, dict)
            or not isinstance(unity_rows, dict)
            or unity_canary.get("serialized_benchmarks") != ["jynew"]
            or unity_canary.get("blocking_benchmarks") != []
            or unity_canary.get("paid_recovery_safe") is not True
            or unity_rows.get("pal3", {}).get("passed") is not True
            or unity_rows.get("jynew", {}).get("editor_overlap_passed") is not True
            or unity_rows.get("jynew", {}).get("mutable_storage_isolated") is not False
        ):
            raise ValueError(
                "C1 background-partition concurrency canary did not pass"
            )
        if (
            source.get("run_id") != "p5-f845d991a94a"
            or source.get("git_commit")
            != "aa5098967387ff07d4e4c1f3775c9346ddc036f8"
            or source.get("harness_hash")
            != "25b4df907d41cb6fedf5ab3f74f05a36d2891360aaa1cf95cf5816e3d2f962aa"
        ):
            raise ValueError("C1 background-partition migration source mismatch")
        rows = [
            (arm, row)
            for arm in C1_ARMS
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict)
        ]
        errors = {
            TrialKey.from_row(arm, row)
            for arm, row in rows
            if row.get("status") == "error"
        }
        expected_error_keys = {
            TrialKey.from_row(str(item["arm"]), item)
            for item in preserved_failures
        }
        if (
            len(rows) != 549
            or sum(row.get("status") == "ok" for _arm, row in rows) != 375
            or sum(row.get("status") == "pending" for _arm, row in rows) != 172
            or errors != expected_error_keys
        ):
            raise ValueError("C1 background-partition migration coverage mismatch")
        row_by_key = {
            TrialKey.from_row(arm, row): row
            for arm, row in rows
        }
        seen_interrupted: set[TrialKey] = set()
        for item in interrupted:
            if not isinstance(item, dict):
                raise ValueError("C1 interrupted-attempt audit row is malformed")
            key = TrialKey.from_row(str(item.get("arm") or ""), item)
            attempt_dir = Path(str(item.get("attempt_dir") or "")).expanduser()
            if (
                key in seen_interrupted
                or row_by_key.get(key, {}).get("status") != "pending"
                or item.get("trial_attempt") != 0
                or item.get("usage_status") != "unknown"
                or item.get("disposition")
                != "quarantine_primary_attempt_and_register_replacement"
                or item.get("recursive_artifact_sha256")
                != _recursive_artifact_sha256(attempt_dir)
            ):
                raise ValueError("C1 interrupted-attempt audit binding mismatch")
            seen_interrupted.add(key)
        source_commit = str(source.get("git_commit") or "")
        current_commit = str(migration.get("current_git_commit") or "")
        completed = __import__("subprocess").run(
            [
                "git",
                "diff",
                "--name-only",
                source_commit,
                current_commit,
                "--",
            ],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("C1 background-partition migration cannot audit the diff")
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        allowed_paths = {
            "docs/c1_concurrency.md",
            "tests/test_c1_concurrency.py",
            "tests/test_c1_task_resources.py",
            "tests/test_c1_trial_persistence.py",
            "tests/test_p5.py",
            "tests/test_runtime_isolation.py",
            "warptest/c1_native_canary.py",
            "warptest/c1_task_resources.py",
            "warptest/c1_unity_canary.py",
            "warptest/gui_agent.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        }
        required_paths = {
            "warptest/c1_native_canary.py",
            "warptest/c1_task_resources.py",
            "warptest/c1_unity_canary.py",
            "warptest/gui_agent.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        }
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            not required_paths.issubset(changed_paths)
            or not set(changed_paths).issubset(allowed_paths)
            or migration.get("source_git_commit") != source_commit
            or current_commit != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256") != changed_paths_sha256
        ):
            raise ValueError("C1 background-partition migration diff binding mismatch")
        return

    if migration.get("migration_id") == "c1_recovery_parallel_scheduler_v1":
        affected_trials = sorted(
            (
                {
                    "arm": str(arm),
                    "model": row.get("model"),
                    "seed": row.get("seed"),
                    "task_id": str(row.get("task_id") or ""),
                    "source_status": str(row.get("status") or ""),
                }
                for arm in source.get("arms_run", [])
                for row in (source.get("arms", {}).get(arm) or [])
                if isinstance(row, dict) and row.get("status") != "ok"
            ),
            key=lambda row: (
                row["arm"],
                str(row["model"]),
                str(row["seed"]),
                row["task_id"],
            ),
        )
        expected_contract = {
            "scheduler_policy": "global_resource_ready_round_robin_v2",
            "recovery_execution": "one_task_fresh_invocation_v1",
            "recovery_dispatch": "two_level_resource_ready_v1",
            "affected_trials": affected_trials,
            "preserved_model_failure_trials": [],
            "registered_harness_failure_trials": [],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "normal_execution_unchanged": True,
            "treatment_semantics_unchanged": True,
        }
        if not affected_trials or any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError(
                "C1 recovery-parallel migration contract is incomplete"
            )
        source_policy = source.get("c1_trial_persistence")
        if (
            not isinstance(source_policy, dict)
            or source_policy.get("recovery_execution")
            != "one_task_fresh_invocation_v1"
        ):
            raise ValueError(
                "C1 recovery-parallel migration source policy is incompatible"
            )
        source_commit = str(source.get("git_commit") or "")
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                "C1 recovery-parallel migration cannot audit the repository diff"
            )
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        allowed_paths = {
            "README.md",
            "docs/c1_concurrency.md",
            "tests/test_c1_concurrency.py",
            "tests/test_c1_trial_persistence.py",
            "tests/test_p5.py",
            "warptest/c1_concurrency.py",
            "warptest/gui_agent.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        }
        required_runtime_paths = {
            "warptest/c1_concurrency.py",
            "warptest/gui_agent.py",
            "warptest/p5_scale.py",
            "warptest/state_loop.py",
        }
        if (
            not required_runtime_paths.issubset(changed_paths)
            or not set(changed_paths).issubset(allowed_paths)
        ):
            raise ValueError(
                "C1 recovery-parallel migration contains unregistered changed paths"
            )
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit")
            != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256")
            != changed_paths_sha256
        ):
            raise ValueError(
                "C1 recovery-parallel migration does not bind the exact diff"
            )
        return

    if migration.get("migration_id") == "c1_resource_ready_scheduler_v2":
        expected_contract = {
            "scheduler_policy": "global_resource_ready_round_robin_v2",
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "treatment_semantics_unchanged": True,
        }
        if any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError(
                "C1 scheduler-only migration contract is incomplete"
            )
        source_commit = str(source.get("git_commit") or "")
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                "C1 scheduler-only migration cannot audit the repository diff"
            )
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        allowed_paths = {
            "README.md",
            "docs/c1_PLAN.md",
            "docs/c1_concurrency.md",
            "tests/test_c1_concurrency.py",
            "tests/test_runtime_isolation.py",
            "warptest/c1_concurrency.py",
            "warptest/c1_sandbox.py",
            "warptest/c1_task_resources.py",
            "warptest/gui_agent.py",
            "warptest/native_input.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
            "warptest/runtime_lock.py",
            "warptest/state_loop.py",
        }
        if not changed_paths or not set(changed_paths).issubset(allowed_paths):
            raise ValueError(
                "C1 scheduler-only migration contains unregistered changed paths"
            )
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(
                changed_paths,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit")
            != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256")
            != changed_paths_sha256
        ):
            raise ValueError(
                "C1 scheduler-only migration does not bind the exact source/current diff"
            )
        return

    if migration.get("migration_id") == "c1_retryable_recovery_ledger_v1":
        affected = [
            {
                "arm": "c1_agent_s",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "libreoffice-impress-executive-restructure",
                "source_status": "error",
            },
            {
                "arm": "c1_agent_s",
                "model": "kimi-k2.6@relay",
                "seed": 0,
                "task_id": "openra-disable-edge-scroll",
                "source_status": "error",
            },
            {
                "arm": "c1_state",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "libreoffice-impress-status-deck",
                "source_status": "error",
            },
            {
                "arm": "c1_state",
                "model": "kimi-k2.6@relay",
                "seed": 0,
                "task_id": "luanti-set-node",
                "source_status": "error",
            },
        ]
        expected_contract = {
            "recovery_ledger_policy": "retryable_infrastructure_top_level_v1",
            "affected_trials": affected,
            "preserved_model_failure_trials": [],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "treatment_semantics_unchanged": True,
        }
        if any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError("C1 retryable-ledger migration contract is incomplete")
        if (
            source.get("run_id") != "p5-4198cb3ebfef"
            or source.get("git_commit")
            != "4033aa6de91647cb4c4ff0fe05b890dd5361ef7c"
            or source.get("harness_hash")
            != "7e423719cc29493b503844ef6ce8831abb439cee83a7cd750d52bde89cc7aa87"
        ):
            raise ValueError("C1 retryable-ledger migration source mismatch")
        rows = [
            (arm, row)
            for arm in C1_ARMS
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict)
        ]
        errors = {
            TrialKey.from_row(arm, row)
            for arm, row in rows
            if row.get("status") == "error"
        }
        affected_keys = {
            TrialKey.from_row(str(row["arm"]), row)
            for row in affected
        }
        if (
            len(rows) != 549
            or sum(row.get("status") == "ok" for _arm, row in rows) != 419
            or sum(row.get("status") == "pending" for _arm, row in rows) != 126
            or errors != affected_keys
            or any(
                row.get("status") == "quarantined_unknown_usage"
                for _arm, row in rows
            )
        ):
            raise ValueError("C1 retryable-ledger migration coverage mismatch")
        source_commit = str(source.get("git_commit") or "")
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("C1 retryable-ledger migration cannot audit the diff")
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        expected_paths = [
            "tests/test_c1_trial_persistence.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
        ]
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            changed_paths != expected_paths
            or migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit") != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256") != changed_paths_sha256
        ):
            raise ValueError("C1 retryable-ledger migration diff binding mismatch")
        return

    if migration.get("migration_id") == "c1_retryable_runtime_marker_v1":
        affected = [
            {
                "arm": "c1_state",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "libreoffice-impress-executive-restructure",
                "source_status": "error",
            }
        ]
        expected_contract = {
            "recovery_ledger_policy": (
                "retryable_infrastructure_missing_marker_v1"
            ),
            "flattened_overhead_policy": (
                "preserve_all_sha_verified_recursive_attempts_v1"
            ),
            "affected_trials": affected,
            "preserved_model_failure_trials": [],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "treatment_semantics_unchanged": True,
        }
        if any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError("C1 retryable-marker migration contract is incomplete")
        if (
            source.get("run_id") != "p5-8f5b4e9120ad"
            or source.get("git_commit")
            != "be82fed06f3def79a4449e6f1a5523b29af99902"
            or source.get("harness_hash")
            != "0053699cc37471f861d7eaad5e7d46b9c62f11651c866516a1ef08f155083066"
        ):
            raise ValueError("C1 retryable-marker migration source mismatch")
        rows = [
            (arm, row)
            for arm in C1_ARMS
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict)
        ]
        expected_errors = {
            (
                "c1_agent_s",
                "gemini-3.5-flash@relay",
                0,
                "luanti-workshop-order-staging",
            ),
            (
                "c1_agent_s",
                "kimi-k2.6@relay",
                0,
                "jupyterlab-notebook-cell-output",
            ),
            (
                "c1_state",
                "gemini-3.5-flash@relay",
                0,
                "libreoffice-impress-executive-restructure",
            ),
        }
        errors = {
            TrialKey.from_row(arm, row)
            for arm, row in rows
            if row.get("status") == "error"
        }
        if (
            len(rows) != 549
            or sum(row.get("status") == "ok" for _arm, row in rows) != 483
            or sum(row.get("status") == "pending" for _arm, row in rows) != 63
            or errors != expected_errors
            or any(
                row.get("status") == "quarantined_unknown_usage"
                for _arm, row in rows
            )
        ):
            raise ValueError("C1 retryable-marker migration coverage mismatch")
        overhead = source.get("c1_operational_overhead") or []
        duplicate_key = TrialKey(
            "c1_state",
            "gemini-3.5-flash@relay",
            0,
            "libreoffice-impress-status-deck",
        )
        duplicate_attempts = {
            row.get("attempt_id")
            for row in overhead
            if isinstance(row, dict)
            and TrialKey.from_row(str(row.get("arm") or ""), row)
            == duplicate_key
        }
        if (
            len(overhead) != 7
            or duplicate_attempts
            != {
                "p5-b2c01ad1eff9:c1_state:gemini-3.5-flash@relay:0:"
                "libreoffice-impress-status-deck:attempt0",
                "p5-4198cb3ebfef:c1_state:gemini-3.5-flash@relay:0:"
                "libreoffice-impress-status-deck:attempt0",
            }
        ):
            raise ValueError("C1 retryable-marker migration overhead mismatch")
        source_commit = str(source.get("git_commit") or "")
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("C1 retryable-marker migration cannot audit the diff")
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        expected_paths = [
            "tests/test_c1_trial_persistence.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
        ]
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            changed_paths != expected_paths
            or migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit") != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256") != changed_paths_sha256
        ):
            raise ValueError("C1 retryable-marker migration diff binding mismatch")
        return

    if migration.get("migration_id") == "c1_exhausted_recovery_terminal_v1":
        affected = [
            {
                "arm": "c1_state",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "libreoffice-impress-executive-restructure",
                "source_status": "error",
            }
        ]
        expected_contract = {
            "recovery_scheduler_policy": "skip_exhausted_exact_usage_failure_v1",
            "affected_trials": [],
            "preserved_model_failure_trials": affected,
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": True,
            "treatment_semantics_unchanged": True,
        }
        if any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError("C1 exhausted-terminal migration contract is incomplete")
        if (
            source.get("run_id") != "p5-16eb039f7cf4"
            or source.get("git_commit")
            != "d958da794879b62ebd80ff75eefad4b89d06c0c2"
            or source.get("harness_hash")
            != "80eeff20b3f5a6f1d14c518db158229a5b07fac12ef105d7d7d8cda61cd07741"
        ):
            raise ValueError("C1 exhausted-terminal migration source mismatch")
        rows = [
            (arm, row)
            for arm in C1_ARMS
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict)
        ]
        expected_error = {
            (
                "c1_state",
                "gemini-3.5-flash@relay",
                0,
                "libreoffice-impress-executive-restructure",
            )
        }
        errors = {
            TrialKey.from_row(arm, row)
            for arm, row in rows
            if row.get("status") == "error"
        }
        if (
            len(rows) != 549
            or sum(row.get("status") == "ok" for _arm, row in rows) != 539
            or sum(row.get("status") == "pending" for _arm, row in rows) != 9
            or errors != expected_error
            or any(
                row.get("status") == "quarantined_unknown_usage"
                for _arm, row in rows
            )
        ):
            raise ValueError("C1 exhausted-terminal migration coverage mismatch")
        terminal_attempts = {
            row.get("trial_attempt")
            for row in (source.get("c1_operational_overhead") or [])
            if isinstance(row, dict)
            and TrialKey.from_row(str(row.get("arm") or ""), row)
            in expected_error
            and row.get("source_provenance") is None
        }
        if terminal_attempts != {0, 1, 2}:
            raise ValueError("C1 exhausted-terminal attempt ledger mismatch")
        source_commit = str(source.get("git_commit") or "")
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("C1 exhausted-terminal migration cannot audit the diff")
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        expected_paths = [
            "tests/test_c1_trial_persistence.py",
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
        ]
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            changed_paths != expected_paths
            or migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit") != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256") != changed_paths_sha256
        ):
            raise ValueError("C1 exhausted-terminal migration diff binding mismatch")
        return

    if migration.get("migration_id") == "c1_state_recovery_settled_v1":
        affected_task_ids = [
            "luanti-mine-bridge-outpost-build",
            "jynew-party-training-roundtrip",
            "pal3-story-party-scene-roundtrip",
            "luanti-self-sufficient-homestead-roundtrip",
            "jynew-sidequest-trade-submap",
            "pal3-world-region-travel-roundtrip",
            "luanti-workshop-order-staging",
            "jynew-battle-ready-loadout",
            "pal3-companion-ending-preparation",
        ]
        affected = [
            {
                "arm": "c1_state",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": task_id,
                "source_status": "ok",
            }
            for task_id in affected_task_ids
        ]
        expected_contract = {
            "settled_report_policy": "sha_verified_terminal_state_schema_rebind_v1",
            "affected_trials": affected,
            "preserved_model_failure_trials": [],
            "completed_trial_import_only": True,
            "unfinished_tuples_requeued": False,
            "treatment_semantics_unchanged": True,
        }
        if any(
            migration.get(field) != value
            for field, value in expected_contract.items()
        ):
            raise ValueError("C1 state-recovery migration contract is incomplete")
        if (
            source.get("run_id") != "p5-e630e64eb662"
            or source.get("git_commit")
            != "42257450e2ed3cab4d33f7b8a682964e184aed91"
            or source.get("harness_hash")
            != "7ffe4a553cac4dd278c473f041a6231b9bde9b7eac244ae16e62fb8567a62393"
        ):
            raise ValueError("C1 state-recovery migration source mismatch")
        rows = [
            (arm, row)
            for arm in C1_ARMS
            for row in (source.get("arms", {}).get(arm) or [])
            if isinstance(row, dict)
        ]
        affected_keys = {
            TrialKey.from_row(str(row["arm"]), row)
            for row in affected
        }
        actual_affected = {
            TrialKey.from_row(arm, row)
            for arm, row in rows
            if row.get("replacement_for")
            and row.get("source_provenance") is None
        }
        if (
            len(rows) != 549
            or sum(row.get("status") == "ok" for _arm, row in rows) != 549
            or actual_affected != affected_keys
            or any(
                row.get("status") == "quarantined_unknown_usage"
                for _arm, row in rows
            )
        ):
            raise ValueError("C1 state-recovery migration coverage mismatch")
        for arm, row in rows:
            key = TrialKey.from_row(arm, row)
            if key not in affected_keys:
                continue
            report_path = Path(str(row.get("report") or "")).expanduser()
            try:
                report_raw = report_path.read_bytes()
                report = json.loads(report_raw)
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    "C1 state-recovery migration evidence is unreadable"
                ) from error
            results = report.get("results") if isinstance(report, dict) else None
            result = (
                results[0]
                if isinstance(results, list)
                and len(results) == 1
                and isinstance(results[0], dict)
                else None
            )
            if (
                row.get("status") != "ok"
                or row.get("report_sha256")
                != hashlib.sha256(report_raw).hexdigest()
                or not isinstance(report, dict)
                or report.get("complete") is not True
                or cost.contains_unknown_usage(report)
                or not isinstance(result, dict)
                or result.get("task_id") != row.get("task_id")
                or result.get("status") not in {"failed", "failure"}
                or result.get("result") is not False
            ):
                raise ValueError(
                    "C1 state-recovery migration evidence is not an exact "
                    "terminal failure"
                )
        source_commit = str(source.get("git_commit") or "")
        completed = __import__("subprocess").run(
            ["git", "diff", "--name-only", source_commit, "--"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError("C1 state-recovery migration cannot audit the diff")
        changed_paths = sorted(
            line for line in completed.stdout.splitlines() if line
        )
        expected_paths = [
            "warptest/p5_aggregate.py",
            "warptest/p5_scale.py",
        ]
        changed_paths_sha256 = hashlib.sha256(
            json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if (
            changed_paths != expected_paths
            or migration.get("source_git_commit") != source_commit
            or migration.get("current_git_commit") != run_context.git_commit(base)
            or migration.get("changed_paths") != changed_paths
            or migration.get("changed_paths_sha256") != changed_paths_sha256
        ):
            raise ValueError("C1 state-recovery migration diff binding mismatch")
        return

    if migration.get("migration_id") == "c1_recovery_sidecar_validation_v1":
        _validate_registered_c1_recovery_resume_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_timeout_usage_snapshot_v1":
        _validate_registered_c1_timeout_usage_snapshot_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_recursive_flattened_lineage_v1":
        _validate_registered_c1_recursive_lineage_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_resume_settled_failure_v1":
        _validate_registered_c1_resume_settled_failure_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_retry_task_plan_resume_v1":
        _validate_registered_c1_retry_task_plan_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_openra_readonly_render_grade_v1":
        _validate_registered_c1_openra_render_grade_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_unity_license_serialization_v1":
        _validate_registered_c1_unity_license_serialization_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_atomic_defer_sidecar_cleanup_v1":
        _validate_registered_c1_atomic_defer_sidecar_cleanup_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return
    if migration.get("migration_id") == "c1_cancelled_ticket_settlement_v1":
        _validate_registered_c1_cancelled_ticket_settlement_migration(
            base=base,
            source=source,
            migration=migration,
        )
        return

    expected_profile = {
        "migration_id": "libreoffice_writer_default_style_alias_v1",
        "source_run_id": "p5-3905122be605",
        "source_git_commit": "b2da5cdc438b5412cee2646358a0881e96ed42fc",
        "source_harness_hash": (
            "749668b812dac6bde95042a71a4573befb4aeb02e4e68f8d29bb96efe77570fe"
        ),
        "affected_trials": [
            {
                "arm": "c1_agent_s_synth",
                "model": "kimi-k2.6@relay",
                "seed": 0,
                "task_id": "libreoffice-writer-board-publication",
                "source_status": "error",
            }
        ],
        "preserved_model_failure_trials": [
            {
                "arm": "c1_agent_s",
                "model": "gpt-5.6-terra@relay",
                "seed": 0,
                "task_id": "2048-cascade-256",
                "source_status": "error",
            },
            {
                "arm": "c1_agent_s",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "libreoffice-calc-forecast-ranking-chart",
                "source_status": "error",
            },
            {
                "arm": "c1_agent_s",
                "model": "kimi-k2.6@relay",
                "seed": 0,
                "task_id": "openra-allied-medium-tank-tech-chain",
                "source_status": "error",
            },
            {
                "arm": "c1_agent_s_synth",
                "model": "gpt-5.6-terra@relay",
                "seed": 0,
                "task_id": "blender-multiobject-scene-assembly",
                "source_status": "error",
            },
        ],
    }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered Writer alias recovery"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 harness migration source provenance mismatch")

    subprocess = __import__("subprocess")
    ast = __import__("ast")

    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 harness migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 harness migration cannot audit the repository diff")
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    allowed_paths = {
        "tests/test_c1_desktop_runtime.py",
        "tests/test_c1_trial_persistence.py",
        "warptest/c1_desktop_runtime.py",
        "warptest/p5_scale.py",
    }
    if changed_paths != allowed_paths:
        raise ValueError(
            "C1 harness migration contains unregistered changed paths: "
            + ", ".join(sorted(changed_paths ^ allowed_paths))
        )

    old_runtime = git_file("warptest/c1_desktop_runtime.py")
    alias_anchor = (
        '_LO_RESULT_HEADER = "C1_DESKTOP_RESULT_V1"\n'
        "\n"
        "\n"
        "def _b64(value: Any) -> str:\n"
    )
    alias_replacement = (
        '_LO_RESULT_HEADER = "C1_DESKTOP_RESULT_V1"\n'
        "_LO_WRITER_STYLE_ALIASES = {\n"
        "    # Writer exposes this localized UI name in prompts and document metadata,\n"
        "    # while the UNO ParaStyleName property requires the stable programmatic name.\n"
        '    "Default Paragraph Style": "Standard",\n'
        "}\n"
        "\n"
        "\n"
        "def _b64(value: Any) -> str:\n"
    )
    helper_anchor = (
        'def _line(kind: str, *values: Any) -> str:\n'
        r'    return "\t".join([kind, *(_b64(value) for value in values)])'
        "\n"
        "\n"
        "\n"
        "def _scalar_parts(value: Any) -> tuple[str, str]:\n"
    )
    helper_replacement = (
        'def _line(kind: str, *values: Any) -> str:\n'
        r'    return "\t".join([kind, *(_b64(value) for value in values)])'
        "\n"
        "\n"
        "\n"
        "def _libreoffice_writer_style_name(value: Any) -> Any:\n"
        "    return _LO_WRITER_STYLE_ALIASES.get(value, value)\n"
        "\n"
        "\n"
        "def _scalar_parts(value: Any) -> tuple[str, str]:\n"
    )
    style_anchor = '                        paragraph.get("style", ""),\n'
    style_replacement = (
        "                        _libreoffice_writer_style_name(\n"
        '                            paragraph.get("style", "")\n'
        "                        ),\n"
    )
    if (
        old_runtime.count(alias_anchor) != 1
        or old_runtime.count(helper_anchor) != 1
        or old_runtime.count(style_anchor) != 1
    ):
        raise ValueError("C1 Writer alias migration source anchors are not exact")
    expected_runtime = (
        old_runtime.replace(alias_anchor, alias_replacement)
        .replace(helper_anchor, helper_replacement)
        .replace(style_anchor, style_replacement)
    )
    current_runtime = (base / "warptest/c1_desktop_runtime.py").read_text(
        encoding="utf-8"
    )
    if current_runtime != expected_runtime:
        raise ValueError("C1 Writer alias runtime patch differs from the registered fix")

    old_p5_tree = ast.parse(git_file("warptest/p5_scale.py"))
    current_p5_tree = ast.parse(
        (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    )
    current_p5_tree.body = [
        node
        for node in current_p5_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {
                "_validate_registered_c1_harness_migration",
                "_validate_registered_c1_recovery_resume_migration",
            }
        )
    ]

    def orchestrator_class(tree: Any) -> Any:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator":
                return node
        raise ValueError("C1 harness migration orchestrator class is missing")

    old_class = orchestrator_class(old_p5_tree)
    current_class = orchestrator_class(current_p5_tree)
    old_methods = {
        child.name: child
        for child in old_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    recovery_only_methods = {
        "_import_c1_source_trials",
        "_validate_source_trial_artifacts",
    }
    if not recovery_only_methods.issubset(old_methods):
        raise ValueError("C1 harness migration recovery methods are missing")
    current_class.body = [
        (
            old_methods[child.name]
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in recovery_only_methods
            )
            else child
        )
        for child in current_class.body
        if not (
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and child.name == "_registered_source_model_failure_kind"
        )
    ]
    if ast.dump(current_p5_tree) != ast.dump(old_p5_tree):
        raise ValueError(
            "C1 harness migration changed p5_scale outside the registered importer"
        )


def _validate_registered_c1_atomic_defer_sidecar_cleanup_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate recovery after an atomic defer left context-only directories."""

    interrupted = [
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "2048-near-loss-rescue-128",
            "source_status": "pending",
            "context_sha256": (
                "7ca88635d743e91f75141fc1f0f471e88e85a3e31d6d70782241a2754b8ace02"
            ),
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "love-office-work-coding",
            "source_status": "pending",
            "context_sha256": (
                "f9f0802cef7d511dd89a78db494b6c147eaf0a9982cd4e5adabf9042af1f054a"
            ),
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "spacerpg-save-restore",
            "source_status": "pending",
            "context_sha256": (
                "63ec99a2a9a34747b962a0f9844fb7a7242cd8a1934a2cf89a1f6be5dbaa02bc"
            ),
        },
    ]
    expected_contract = {
        "migration_id": "c1_atomic_defer_sidecar_cleanup_v1",
        "deferral_cleanup_policy": "remove_context_only_attempt_before_requeue_v1",
        "interrupted_context_only_trials": interrupted,
        "affected_trials": [],
        "registered_harness_failure_trials": [],
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
    }
    if any(
        migration.get(field) != value
        for field, value in expected_contract.items()
    ):
        raise ValueError("C1 atomic-defer migration contract is incomplete")
    if (
        source.get("run_id") != "p5-c7ca9b5697dc"
        or source.get("git_commit")
        != "fa275261fa69fa0de0ad4dbdb7bcc30f65a29b4e"
        or source.get("harness_hash")
        != "d564f4b54b00611fa9c0491280508e3b5790a445e2032811b4844778fcbe3dd3"
    ):
        raise ValueError("C1 atomic-defer migration source mismatch")

    source_path = Path(str(migration.get("source_index") or "")).expanduser()
    source_raw = source_path.read_bytes()
    source_sha = hashlib.sha256(source_raw).hexdigest()
    if source_sha != migration.get("source_index_sha256"):
        raise ValueError("C1 atomic-defer source index SHA mismatch")
    persisted = json.loads(source_raw)
    upstream = persisted.get("c1_sources") if isinstance(persisted, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 atomic-defer source must bind one upstream run")
    expected_source = _flatten_persisted_c1_source_for_lineage(
        source_path=source_path,
        source=persisted,
        source_sha=source_sha,
    )
    if source != expected_source:
        raise ValueError("C1 atomic-defer flattened source mismatch")
    upstream_record = upstream[0]
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(field) != migration.get(f"upstream_{field}")
        for field in ("source_index", "source_index_sha256", "source_run_id")
    ):
        raise ValueError("C1 atomic-defer upstream provenance mismatch")

    rows = [
        (arm, row)
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 506
        or sum(row.get("status") == "pending" for _arm, row in rows) != 43
        or any(
            row.get("status") not in {"ok", "pending"}
            for _arm, row in rows
        )
    ):
        raise ValueError("C1 atomic-defer source coverage differs from 506/549")

    expected_failures = {
        (7, "c1_agent_s", "2048-near-loss-rescue-128"),
        (8, "c1_agent_s_synth", "love-office-work-coding"),
    }
    actual_failures: set[tuple[int, str, str]] = set()
    for row in source.get("execution_order") or []:
        if not isinstance(row, dict) or row.get("integrity_failure") is not True:
            continue
        message = str(row.get("error") or "")
        task_id = (
            "2048-near-loss-rescue-128"
            if "/0842131c4582055a/attempt0" in message
            else (
                "love-office-work-coding"
                if "/d45f34949feae84d/attempt0" in message
                else ""
            )
        )
        if (
            row.get("status") != "failed"
            or "C1 invocation directory already exists" not in message
        ):
            raise ValueError("C1 atomic-defer failure classification mismatch")
        actual_failures.add((int(row["ordinal"]), str(row["arm"]), task_id))
    if actual_failures != expected_failures:
        raise ValueError("C1 atomic-defer failure rows mismatch")

    row_by_key = {
        TrialKey.from_row(arm, row): row
        for arm, row in rows
    }
    for item in interrupted:
        key = TrialKey.from_row(str(item["arm"]), item)
        row = row_by_key.get(key)
        digest = hashlib.sha256(str(item["task_id"]).encode("utf-8")).hexdigest()[:16]
        attempt_dirs = list(
            (source_path.parent / str(item["arm"])).glob(
                f"*/seed{item['seed']}/trials/{digest}/attempt0"
            )
        )
        if row is None or row.get("status") != "pending" or len(attempt_dirs) != 1:
            raise ValueError("C1 atomic-defer pending sidecar binding mismatch")
        context_path = attempt_dirs[0] / _C1_INVOCATION_CONTEXT_FILE
        files = [path for path in attempt_dirs[0].iterdir() if path.is_file()]
        if (
            files != [context_path]
            or hashlib.sha256(context_path.read_bytes()).hexdigest()
            != item["context_sha256"]
        ):
            raise ValueError("C1 atomic-defer context-only evidence mismatch")

    subprocess = __import__("subprocess")
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--ignore-submodules=dirty",
            "--name-only",
            str(source.get("git_commit") or ""),
            "--",
        ],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    changed_paths = sorted(line for line in changed.stdout.splitlines() if line)
    expected_paths = [
        "tests/test_c1_concurrency.py",
        "tests/test_c1_task_resources.py",
        "tests/test_c1_trial_persistence.py",
        "warptest/c1_concurrency.py",
        "warptest/c1_task_resources.py",
        "warptest/p5_scale.py",
    ]
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        changed.returncode != 0
        or changed_paths != expected_paths
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
        or migration.get("current_git_commit") != run_context.git_commit(base)
    ):
        raise ValueError("C1 atomic-defer migration diff binding mismatch")
    current_p5 = (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    if (
        current_p5.count("deferred C1 invocation left unexpected attempt artifacts")
        != 2
        or current_p5.count("context_path.unlink()") != 2
        or current_p5.count("invocation_dir.rmdir()") != 2
    ):
        raise ValueError("C1 atomic-defer cleanup patch mismatch")


def _validate_registered_c1_unity_license_serialization_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate cross-version Unity license-client serialization recovery."""

    harness_failure = {
        "arm": "c1_agent_s",
        "model": "kimi-k2.6@relay",
        "seed": 0,
        "task_id": "pal3-world-region-travel-roundtrip",
        "source_status": "error",
    }
    expected_contract = {
        "migration_id": "c1_unity_license_serialization_v1",
        "resource_policy": "shared_unity_license_client_v1",
        "affected_trials": [],
        "registered_harness_failure_trials": [harness_failure],
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "recursive_lineage_preserved": True,
        "treatment_semantics_unchanged": True,
    }
    if any(
        migration.get(field) != value
        for field, value in expected_contract.items()
    ):
        raise ValueError("C1 Unity-license migration contract is incomplete")
    if (
        source.get("run_id") != "p5-e1641b76d103"
        or source.get("git_commit")
        != "fa275261fa69fa0de0ad4dbdb7bcc30f65a29b4e"
        or source.get("harness_hash")
        != "afd64147b04445b31e8b7cce880daf391e403d213b3c883b53fe21e2778499c6"
    ):
        raise ValueError("C1 Unity-license migration source mismatch")

    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    if not recovery_path.is_file():
        raise ValueError("C1 Unity-license recovery index is missing")
    recovery_raw = recovery_path.read_bytes()
    recovery_sha = hashlib.sha256(recovery_raw).hexdigest()
    if recovery_sha != migration.get("recovery_index_sha256"):
        raise ValueError("C1 Unity-license recovery index SHA mismatch")
    try:
        recovery = json.loads(recovery_raw)
    except json.JSONDecodeError as error:
        raise ValueError("C1 Unity-license recovery index is unreadable") from error
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 Unity-license recovery must bind one upstream run")
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": recovery_sha,
        "upstream_sources": copy.deepcopy(upstream),
    }
    expected_source = copy.deepcopy(recovery)
    expected_source.pop("c1_sources", None)
    expected_source["flattened_source_lineage"] = expected_lineage
    if source != expected_source:
        raise ValueError("C1 Unity-license flattened source differs from recovery")
    upstream_record = upstream[0]
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(field) != migration.get(f"upstream_{field}")
        for field in ("source_index", "source_index_sha256", "source_run_id")
    ):
        raise ValueError("C1 Unity-license upstream provenance mismatch")

    rows = [
        (arm, row)
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    error_keys = {
        TrialKey.from_row(arm, row)
        for arm, row in rows
        if row.get("status") == "error"
    }
    expected_errors = {
        TrialKey(
            "c1_agent_s",
            "kimi-k2.6@relay",
            0,
            "openra-target-lines-automatic",
        ),
        TrialKey(
            "c1_agent_s",
            "kimi-k2.6@relay",
            0,
            "pal3-world-region-travel-roundtrip",
        ),
        TrialKey(
            "c1_agent_s_synth",
            "kimi-k2.6@relay",
            0,
            "jynew-save-restore",
        ),
        TrialKey(
            "c1_agent_s_synth",
            "kimi-k2.6@relay",
            0,
            "luanti-replace-node",
        ),
    }
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 498
        or sum(row.get("status") == "pending" for _arm, row in rows) != 47
        or error_keys != expected_errors
        or any(
            row.get("status") == "quarantined_unknown_usage"
            for _arm, row in rows
        )
    ):
        raise ValueError("C1 Unity-license source coverage differs from 502/549")

    source_commit = str(source.get("git_commit") or "")
    subprocess = __import__("subprocess")
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--ignore-submodules=dirty",
            "--name-only",
            source_commit,
            "--",
        ],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 Unity-license migration cannot audit the diff")
    changed_paths = sorted(line for line in changed.stdout.splitlines() if line)
    expected_paths = [
        "tests/test_c1_concurrency.py",
        "tests/test_c1_task_resources.py",
        "tests/test_c1_trial_persistence.py",
        "warptest/c1_concurrency.py",
        "warptest/c1_task_resources.py",
        "warptest/p5_scale.py",
    ]
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        changed_paths != expected_paths
        or migration.get("source_git_commit") != source_commit
        or migration.get("current_git_commit") != run_context.git_commit(base)
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
    ):
        raise ValueError("C1 Unity-license migration diff binding mismatch")

    old_resources = subprocess.run(
        ["git", "show", f"{source_commit}:warptest/c1_task_resources.py"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    expected_resources = old_resources.replace(
        'LEGACY_NATIVE_INPUT_LOCK_TARGET = Path("/tmp/warptest-c1-luanti.lock")\n',
        'LEGACY_NATIVE_INPUT_LOCK_TARGET = Path("/tmp/warptest-c1-luanti.lock")\n'
        'UNITY_LICENSE_LOCK_TARGET = Path("/tmp/warptest-c1-unity-license.lock")\n',
        1,
    )
    old_targets = '''    unity_targets = (
        (unity_serial_lease_target(normalized),)
        if normalized in serialized
        else ()
    )
'''
    new_targets = '''    unity_targets = (
        (UNITY_LICENSE_LOCK_TARGET,)
        if normalized in UNITY_BENCHMARKS
        else ()
    ) + (
        (unity_serial_lease_target(normalized),)
        if normalized in serialized
        else ()
    )
'''
    expected_resources = expected_resources.replace(old_targets, new_targets, 1)
    if (
        expected_resources == old_resources
        or (base / "warptest/c1_task_resources.py").read_text(encoding="utf-8")
        != expected_resources
    ):
        raise ValueError(
            "C1 Unity-license migration changed task resources outside the lock"
        )


def _validate_registered_c1_cancelled_ticket_settlement_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the treatment-neutral cancelled-ticket settlement repair."""

    harness_failure = {
        "arm": "c1_agent_s",
        "model": "kimi-k2.6@relay",
        "seed": 0,
        "task_id": "openra-target-lines-automatic",
        "source_status": "error",
    }
    preserved_model_failure = {
        "arm": "c1_agent_s",
        "model": "gpt-5.6-terra@relay",
        "seed": 0,
        "task_id": "pal3-companion-ending-preparation",
        "source_status": "error",
    }
    expected_contract = {
        "migration_id": "c1_cancelled_ticket_settlement_v1",
        "affected_trials": [],
        "registered_harness_failure_trials": [harness_failure],
        "preserved_model_failure_trials": [preserved_model_failure],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
    }
    if any(
        migration.get(field) != value
        for field, value in expected_contract.items()
    ):
        raise ValueError(
            "C1 cancelled-ticket migration contract is incomplete"
        )
    if (
        source.get("run_id") != "p5-1ef41c8ccc98"
        or source.get("git_commit")
        != "fa275261fa69fa0de0ad4dbdb7bcc30f65a29b4e"
        or source.get("harness_hash")
        != "4bf5083ee562b3aed5ba1dc861b1399bdf7b44c66e3f4b2a32836e61f0052065"
    ):
        raise ValueError("C1 cancelled-ticket migration source mismatch")

    rows = [
        (arm, row)
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = [
        {
            "arm": arm,
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": row.get("task_id"),
            "source_status": "error",
        }
        for arm, row in rows
        if row.get("status") == "error"
    ]
    if (
        len(rows) != 549
        or sum(row.get("status") == "ok" for _arm, row in rows) != 451
        or sum(row.get("status") == "pending" for _arm, row in rows) != 96
        or errors != [preserved_model_failure, harness_failure]
        or any(
            row.get("status") == "quarantined_unknown_usage"
            for _arm, row in rows
        )
    ):
        raise ValueError(
            "C1 cancelled-ticket source coverage differs from 453/549"
        )

    source_commit = str(source.get("git_commit") or "")
    subprocess = __import__("subprocess")

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 cancelled-ticket migration cannot read {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        [
            "git",
            "diff",
            "--ignore-submodules=dirty",
            "--name-only",
            source_commit,
            "--",
        ],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 cancelled-ticket migration cannot audit the diff")
    changed_paths = sorted(line for line in changed.stdout.splitlines() if line)
    expected_paths = [
        "tests/test_c1_concurrency.py",
        "warptest/c1_concurrency.py",
        "warptest/p5_scale.py",
    ]
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        changed_paths != expected_paths
        or migration.get("source_git_commit") != source_commit
        or migration.get("current_git_commit") != run_context.git_commit(base)
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
    ):
        raise ValueError("C1 cancelled-ticket migration diff binding mismatch")

    old_concurrency = git_file("warptest/c1_concurrency.py")
    old_cancel = '''        self.handle.complete(ticket, status="cancelled")
        self.stop(
'''
    new_cancel = '''        # Another lane may have stopped the invocation after this lane claimed
        # a ticket but before it admitted the task. cancel_invocation() already
        # settles that claimed ticket, so completing it again would turn a
        # normal sibling cancellation into a ValueError at the outer layer.
        if ticket.state != "cancelled":
            self.handle.complete(ticket, status="cancelled")
        self.stop(
'''
    expected_concurrency = old_concurrency.replace(old_cancel, new_cancel, 1)
    if (
        expected_concurrency == old_concurrency
        or (base / "warptest/c1_concurrency.py").read_text(encoding="utf-8")
        != expected_concurrency
    ):
        raise ValueError(
            "C1 cancelled-ticket migration changed concurrency outside settlement"
        )

    old_p5 = git_file("warptest/p5_scale.py")
    old_rebind = '''        harness_rebind = set(mismatches) == {"git_commit", "harness_hash"} or (
            application_revisions_rebind
        )
'''
    new_rebind = '''        harness_rebind = set(mismatches) in (
            {"harness_hash"},
            {"git_commit", "harness_hash"},
        ) or application_revisions_rebind
'''
    before_rebind, separator, after_rebind = old_p5.rpartition(old_rebind)
    if not separator:
        raise ValueError("C1 cancelled-ticket migration rebind patch is missing")
    expected_p5 = before_rebind + new_rebind + after_rebind
    ast = __import__("ast")
    expected_tree = ast.parse(expected_p5)
    current_tree = ast.parse(
        (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    )
    expected_dispatcher = next(
        node
        for node in expected_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_validate_registered_c1_harness_migration"
    )
    current_tree.body = [
        expected_dispatcher
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_validate_registered_c1_harness_migration"
        else node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            == "_validate_registered_c1_cancelled_ticket_settlement_migration"
        )
    ]
    if ast.dump(current_tree) != ast.dump(expected_tree):
        raise ValueError(
            "C1 cancelled-ticket migration changed p5_scale outside recovery"
        )


def _validate_registered_c1_recovery_resume_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the treatment-neutral recovery-resume lineage migration."""

    preserved_model_failures = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-cascade-256",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-calc-forecast-ranking-chart",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-medium-tank-tech-chain",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "blender-multiobject-scene-assembly",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-multistage-win-2048",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
    ]
    expected_profile = {
        "migration_id": "c1_recovery_sidecar_validation_v1",
        "source_run_id": "p5-4c1d721a10da",
        "source_git_commit": "87cd51ba449ce1941af7d2b4ad442c8e0698a378",
        "source_harness_hash": (
            "321365797f93433054f4857b5047507d15fba26963526e0acdb6ab271e2e5cbc"
        ),
        "affected_trials": [],
        "preserved_model_failure_trials": preserved_model_failures,
    }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered recovery-resume fix"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 recovery-resume migration source provenance mismatch")

    lineage = source.get("flattened_source_lineage")
    if not isinstance(lineage, dict) or any(
        lineage.get(key) != value
        for key, value in {
            "schema_version": 1,
            "kind": "c1_flattened_recovery_source",
            "derived_from_index": migration.get("recovery_index"),
            "derived_from_index_sha256": migration.get(
                "recovery_index_sha256"
            ),
        }.items()
    ):
        raise ValueError("C1 flattened recovery lineage is malformed")
    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    if not recovery_path.is_file():
        raise ValueError("C1 recovery-resume source index is missing")
    recovery_raw = recovery_path.read_bytes()
    if (
        hashlib.sha256(recovery_raw).hexdigest()
        != migration.get("recovery_index_sha256")
    ):
        raise ValueError("C1 recovery-resume source index SHA mismatch")
    try:
        recovery = json.loads(recovery_raw)
    except json.JSONDecodeError as error:
        raise ValueError("C1 recovery-resume source index is unreadable") from error
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 recovery-resume source must bind exactly one upstream run")
    upstream_record = upstream[0]
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": hashlib.sha256(recovery_raw).hexdigest(),
        "upstream_sources": copy.deepcopy(upstream),
    }
    if lineage != expected_lineage:
        raise ValueError("C1 flattened recovery lineage differs from its source index")
    expected_source = copy.deepcopy(recovery)
    expected_source.pop("c1_sources", None)
    expected_source["flattened_source_lineage"] = expected_lineage
    if source != expected_source:
        raise ValueError("C1 flattened recovery source was not derived exactly")
    expected_upstream = {
        "source_index": migration.get("upstream_source_index"),
        "source_index_sha256": migration.get("upstream_source_index_sha256"),
        "source_run_id": migration.get("upstream_source_run_id"),
    }
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(key) != value for key, value in expected_upstream.items()
    ):
        raise ValueError("C1 recovery-resume upstream provenance mismatch")
    if (
        migration.get("recovery_run_id") != recovery.get("run_id")
        or migration.get("recovery_git_commit") != recovery.get("git_commit")
        or migration.get("recovery_harness_hash") != recovery.get("harness_hash")
    ):
        raise ValueError("C1 recovery-resume contract does not bind the recovery run")

    rows = [
        row
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    if (
        len(rows) != 270
        or sum(row.get("status") != "pending" for row in rows) != 241
        or sum(row.get("status") == "pending" for row in rows) != 29
        or any(row.get("status") == "quarantined_unknown_usage" for row in rows)
    ):
        raise ValueError("C1 recovery-resume source coverage differs from 241/270")

    subprocess = __import__("subprocess")
    ast = __import__("ast")
    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 recovery-resume migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 recovery-resume migration cannot audit the repository diff")
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    allowed_paths = {
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_scale.py",
    }
    if changed_paths != allowed_paths:
        raise ValueError(
            "C1 recovery-resume migration contains unregistered changed paths: "
            + ", ".join(sorted(changed_paths ^ allowed_paths))
        )

    old_tree = ast.parse(git_file("warptest/p5_scale.py"))
    current_tree = ast.parse(
        (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    )
    old_tree.body = [
        node
        for node in old_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_validate_registered_c1_harness_migration"
        )
    ]
    current_tree.body = [
        node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name
            in {
                "_validate_registered_c1_harness_migration",
                "_validate_registered_c1_recovery_resume_migration",
            }
        )
    ]

    def orchestrator_class(tree: Any) -> Any:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator":
                return node
        raise ValueError("C1 recovery-resume orchestrator class is missing")

    old_class = orchestrator_class(old_tree)
    current_class = orchestrator_class(current_tree)
    old_methods = {
        child.name: child
        for child in old_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    current_methods = {
        child.name: child
        for child in current_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    validator_name = "_validate_existing_replacement_row"
    old_validator = copy.deepcopy(old_methods[validator_name])
    expected_validator = copy.deepcopy(old_validator)
    inserted = False
    for node in ast.walk(expected_validator):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Dict):
            continue
        keys = [
            key.value if isinstance(key, ast.Constant) else None
            for key in node.value.keys
        ]
        if "grounder_model" not in keys or "synth_model" not in keys:
            continue
        synth_index = keys.index("synth_model")
        node.value.keys.insert(synth_index, ast.Constant("provider_timeout_s"))
        node.value.values.insert(
            synth_index,
            ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()),
                attr="provider_timeout_s",
                ctx=ast.Load(),
            ),
        )
        inserted = True
        break
    if not inserted or ast.dump(expected_validator) != ast.dump(
        current_methods[validator_name]
    ):
        raise ValueError(
            "C1 recovery-resume sidecar validator differs from the registered fix"
        )

    recovery_only_methods = {
        "_import_c1_source_trials",
        "_validate_source_trial_artifacts",
        validator_name,
    }
    current_class.body = [
        (
            old_methods[child.name]
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in recovery_only_methods
            )
            else child
        )
        for child in current_class.body
    ]
    if ast.dump(current_tree) != ast.dump(old_tree):
        raise ValueError(
            "C1 recovery-resume migration changed execution code outside recovery"
        )
def _validate_registered_c1_timeout_usage_snapshot_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the treatment-neutral timeout-ledger snapshot migration."""

    existing_failures = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-cascade-256",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-calc-forecast-ranking-chart",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-medium-tank-tech-chain",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "blender-multiobject-scene-assembly",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-multistage-win-2048",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
    ]
    new_timeouts = [
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jupyterlab-reproducible-experiment-workspace",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "luanti-self-sufficient-homestead-roundtrip",
            "source_status": "error",
        },
    ]
    expected_profile = {
        "migration_id": "c1_timeout_usage_snapshot_v1",
        "source_run_id": "p5-ef402dd2a30c",
        "source_git_commit": "bd521c0c21e8328defeda91c830a0f08edb3075c",
        "source_harness_hash": (
            "bcf86c600e3d2dd8ceae04915d31d0457deafccf5bea9022cb6a7615547c4765"
        ),
        "affected_trials": new_timeouts,
        "preserved_model_failure_trials": existing_failures + new_timeouts,
    }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered timeout snapshot fix"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 timeout snapshot migration source provenance mismatch")

    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    if not recovery_path.is_file():
        raise ValueError("C1 timeout snapshot source index is missing")
    recovery_raw = recovery_path.read_bytes()
    if hashlib.sha256(recovery_raw).hexdigest() != migration.get(
        "recovery_index_sha256"
    ):
        raise ValueError("C1 timeout snapshot source index SHA mismatch")
    try:
        recovery = json.loads(recovery_raw)
    except json.JSONDecodeError as error:
        raise ValueError("C1 timeout snapshot source index is unreadable") from error
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 timeout snapshot source must bind one upstream run")
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": hashlib.sha256(recovery_raw).hexdigest(),
        "upstream_sources": copy.deepcopy(upstream),
    }
    if source.get("flattened_source_lineage") != expected_lineage:
        raise ValueError("C1 timeout snapshot flattened lineage is malformed")
    expected_source = copy.deepcopy(recovery)
    expected_source.pop("c1_sources", None)
    expected_source["flattened_source_lineage"] = expected_lineage
    if source != expected_source:
        raise ValueError("C1 timeout snapshot source was not derived exactly")
    upstream_record = upstream[0]
    expected_upstream = {
        "source_index": migration.get("upstream_source_index"),
        "source_index_sha256": migration.get("upstream_source_index_sha256"),
        "source_run_id": migration.get("upstream_source_run_id"),
    }
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(key) != value for key, value in expected_upstream.items()
    ):
        raise ValueError("C1 timeout snapshot upstream provenance mismatch")
    if (
        migration.get("recovery_run_id") != recovery.get("run_id")
        or migration.get("recovery_git_commit") != recovery.get("git_commit")
        or migration.get("recovery_harness_hash") != recovery.get("harness_hash")
    ):
        raise ValueError("C1 timeout snapshot contract does not bind the recovery run")

    rows = [
        row
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = [
        {
            "arm": arm,
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": row.get("task_id"),
            "source_status": "error",
        }
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    if (
        len(rows) != 270
        or sum(row.get("status") == "ok" for row in rows) != 244
        or sum(row.get("status") == "pending" for row in rows) != 24
        or errors != new_timeouts
        or any(row.get("status") == "quarantined_unknown_usage" for row in rows)
    ):
        raise ValueError("C1 timeout snapshot source coverage differs from 244/270")

    subprocess = __import__("subprocess")
    ast = __import__("ast")
    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 timeout snapshot migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 timeout snapshot migration cannot audit repository diff")
    allowed_paths = {
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_scale.py",
    }
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    if changed_paths != allowed_paths:
        raise ValueError(
            "C1 timeout snapshot migration contains unregistered changed paths: "
            + ", ".join(sorted(changed_paths ^ allowed_paths))
        )

    old_tree = ast.parse(git_file("warptest/p5_scale.py"))
    current_tree = ast.parse(
        (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    )
    ignored_functions = {
        "_validate_registered_c1_harness_migration",
        "_validate_registered_c1_recovery_resume_migration",
        "_validate_registered_c1_timeout_usage_snapshot_migration",
    }
    old_tree.body = [
        node
        for node in old_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ignored_functions
        )
    ]
    current_tree.body = [
        node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ignored_functions
        )
    ]

    def orchestrator_class(tree: Any) -> Any:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator":
                return node
        raise ValueError("C1 timeout snapshot orchestrator class is missing")

    old_class = orchestrator_class(old_tree)
    current_class = orchestrator_class(current_tree)
    old_methods = {
        child.name: child
        for child in old_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    current_methods = {
        child.name: child
        for child in current_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    record_name = "_record"
    expected_record = copy.deepcopy(old_methods[record_name])
    replacement_count = 0
    for node in ast.walk(expected_record):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value if isinstance(key, ast.Constant) else None for key in node.keys
        ]
        if "known_and_unknown_usage" not in keys or "reason" not in keys:
            continue
        index = keys.index("known_and_unknown_usage")
        node.values[index] = ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()),
                attr="_unknown_usage_snapshot",
                ctx=ast.Load(),
            ),
            args=[ast.Name(id="report", ctx=ast.Load())],
            keywords=[],
        )
        replacement_count += 1
    if replacement_count != 1 or ast.dump(expected_record) != ast.dump(
        current_methods[record_name]
    ):
        raise ValueError(
            "C1 timeout snapshot writer differs from the registered fix"
        )

    current_class.body = [
        (
            old_methods[child.name]
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == record_name
            )
            else child
        )
        for child in current_class.body
    ]
    if ast.dump(current_tree) != ast.dump(old_tree):
        raise ValueError(
            "C1 timeout snapshot migration changed execution code outside persistence"
        )


def _validate_registered_c1_recursive_lineage_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the treatment-neutral recursive recovery-lineage migration."""

    preserved_failures = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-cascade-256",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-calc-forecast-ranking-chart",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-medium-tank-tech-chain",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "blender-multiobject-scene-assembly",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-multistage-win-2048",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jupyterlab-reproducible-experiment-workspace",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "luanti-self-sufficient-homestead-roundtrip",
            "source_status": "error",
        },
    ]
    expected_profile = {
        "migration_id": "c1_recursive_flattened_lineage_v1",
        "source_run_id": "p5-ef402dd2a30c",
        "source_git_commit": "bd521c0c21e8328defeda91c830a0f08edb3075c",
        "source_harness_hash": (
            "bcf86c600e3d2dd8ceae04915d31d0457deafccf5bea9022cb6a7615547c4765"
        ),
        "affected_trials": [],
        "preserved_model_failure_trials": preserved_failures,
    }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered recursive "
            "lineage fix"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 recursive lineage migration source provenance mismatch")

    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    if not recovery_path.is_file():
        raise ValueError("C1 recursive lineage source index is missing")
    recovery_raw = recovery_path.read_bytes()
    if hashlib.sha256(recovery_raw).hexdigest() != migration.get(
        "recovery_index_sha256"
    ):
        raise ValueError("C1 recursive lineage source index SHA mismatch")
    try:
        recovery = json.loads(recovery_raw)
    except json.JSONDecodeError as error:
        raise ValueError("C1 recursive lineage source index is unreadable") from error
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 recursive lineage source must bind one upstream run")
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": hashlib.sha256(recovery_raw).hexdigest(),
        "upstream_sources": copy.deepcopy(upstream),
    }
    if source.get("flattened_source_lineage") != expected_lineage:
        raise ValueError("C1 recursive flattened lineage is malformed")
    expected_source = copy.deepcopy(recovery)
    expected_source.pop("c1_sources", None)
    expected_source["flattened_source_lineage"] = expected_lineage
    if source != expected_source:
        raise ValueError("C1 recursive source was not derived exactly")
    upstream_record = upstream[0]
    expected_upstream = {
        "source_index": migration.get("upstream_source_index"),
        "source_index_sha256": migration.get("upstream_source_index_sha256"),
        "source_run_id": migration.get("upstream_source_run_id"),
    }
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(key) != value for key, value in expected_upstream.items()
    ):
        raise ValueError("C1 recursive lineage upstream provenance mismatch")
    if (
        migration.get("recovery_run_id") != recovery.get("run_id")
        or migration.get("recovery_git_commit") != recovery.get("git_commit")
        or migration.get("recovery_harness_hash") != recovery.get("harness_hash")
    ):
        raise ValueError("C1 recursive lineage contract does not bind the recovery run")

    rows = [
        row
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = [
        {
            "arm": arm,
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": row.get("task_id"),
            "source_status": "error",
        }
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    if (
        len(rows) != 270
        or sum(row.get("status") == "ok" for row in rows) != 244
        or sum(row.get("status") == "pending" for row in rows) != 24
        or errors != preserved_failures[-2:]
        or any(row.get("status") == "quarantined_unknown_usage" for row in rows)
    ):
        raise ValueError("C1 recursive lineage source coverage differs from 244/270")

    subprocess = __import__("subprocess")
    ast = __import__("ast")
    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 recursive lineage migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 recursive lineage migration cannot audit repository diff")
    allowed_paths = {
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_scale.py",
    }
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    if changed_paths != allowed_paths:
        raise ValueError(
            "C1 recursive lineage migration contains unregistered changed paths: "
            + ", ".join(sorted(changed_paths ^ allowed_paths))
        )

    old_tree = ast.parse(git_file("warptest/p5_scale.py"))
    current_tree = ast.parse(
        (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    )
    ignored_functions = {
        "_validate_registered_c1_harness_migration",
        "_validate_registered_c1_recovery_resume_migration",
        "_validate_registered_c1_timeout_usage_snapshot_migration",
        "_validate_registered_c1_recursive_lineage_migration",
    }
    old_tree.body = [
        node
        for node in old_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ignored_functions
        )
    ]
    current_tree.body = [
        node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ignored_functions
        )
    ]

    def orchestrator_class(tree: Any) -> Any:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator":
                return node
        raise ValueError("C1 recursive lineage orchestrator class is missing")

    old_class = orchestrator_class(old_tree)
    current_class = orchestrator_class(current_tree)
    old_methods = {
        child.name: child
        for child in old_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    current_methods = {
        child.name: child
        for child in current_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    record_name = "_record"
    expected_record = copy.deepcopy(old_methods[record_name])
    replacement_count = 0
    for node in ast.walk(expected_record):
        if not isinstance(node, ast.Dict):
            continue
        keys = [
            key.value if isinstance(key, ast.Constant) else None for key in node.keys
        ]
        if "known_and_unknown_usage" not in keys or "reason" not in keys:
            continue
        index = keys.index("known_and_unknown_usage")
        node.values[index] = ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="self", ctx=ast.Load()),
                attr="_unknown_usage_snapshot",
                ctx=ast.Load(),
            ),
            args=[ast.Name(id="report", ctx=ast.Load())],
            keywords=[],
        )
        replacement_count += 1
    if replacement_count != 1 or ast.dump(expected_record) != ast.dump(
        current_methods[record_name]
    ):
        raise ValueError(
            "C1 recursive lineage migration contains an unregistered writer change"
        )

    recovery_only_methods = {record_name, "_import_c1_source_trials"}
    current_class.body = [
        (
            old_methods[child.name]
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name in recovery_only_methods
            )
            else child
        )
        for child in current_class.body
    ]
    if ast.dump(current_tree) != ast.dump(old_tree):
        raise ValueError(
            "C1 recursive lineage migration changed execution code outside recovery"
        )


def _validate_registered_c1_resume_settled_failure_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the treatment-neutral settled-failure resume migration."""

    preserved_failures = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-cascade-256",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gemini-3.5-flash@relay",
            "seed": 0,
            "task_id": "libreoffice-calc-forecast-ranking-chart",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-medium-tank-tech-chain",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "blender-multiobject-scene-assembly",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "2048-multistage-win-2048",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "kimi-k2.6@relay",
            "seed": 0,
            "task_id": "jupyterlab-reproducible-experiment-workspace",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "luanti-self-sufficient-homestead-roundtrip",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "luanti-workshop-order-staging",
            "source_status": "error",
        },
    ]
    expected_coverage = (270, 256, 13)
    expected_errors = preserved_failures[-1:]
    if source.get("run_id") == "p5-f5a2dcca1df7":
        preserved_failures = [
            {
                "arm": "c1_state",
                "model": "gemini-3.5-flash@relay",
                "seed": 0,
                "task_id": "luanti-replace-node",
                "source_status": "error",
            }
        ]
        expected_profile = {
            "migration_id": "c1_resume_settled_failure_v1",
            "source_run_id": "p5-f5a2dcca1df7",
            "source_git_commit": "3719208e1b2cd755114fc005752fc5225aeded4d",
            "source_harness_hash": (
                "a141e52b0d7b08499b477b016d26dfd89e8d896862df8f3279044aad57e8d732"
            ),
            "affected_trials": [],
            "preserved_model_failure_trials": preserved_failures,
        }
        expected_coverage = (549, 512, 36)
        expected_errors = preserved_failures
    else:
        expected_profile = {
            "migration_id": "c1_resume_settled_failure_v1",
            "source_run_id": "p5-6ac4dddb5ae9",
            "source_git_commit": "f7bbf7618abd73b2c6022cd9d6c3dc1e49473a75",
            "source_harness_hash": (
                "0c5538bed096c6779d2b0eb5854c4eeeb1e75c3a74c243d765e3e1b061ec8f33"
            ),
            "affected_trials": [],
            "preserved_model_failure_trials": preserved_failures,
        }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered settled-failure "
            "resume fix"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 settled-failure migration source provenance mismatch")

    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    if not recovery_path.is_file():
        raise ValueError("C1 settled-failure source index is missing")
    recovery_raw = recovery_path.read_bytes()
    if hashlib.sha256(recovery_raw).hexdigest() != migration.get(
        "recovery_index_sha256"
    ):
        raise ValueError("C1 settled-failure source index SHA mismatch")
    try:
        recovery = json.loads(recovery_raw)
    except json.JSONDecodeError as error:
        raise ValueError("C1 settled-failure source index is unreadable") from error
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 settled-failure source must bind one upstream run")
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": hashlib.sha256(recovery_raw).hexdigest(),
        "upstream_sources": copy.deepcopy(upstream),
    }
    if source.get("flattened_source_lineage") != expected_lineage:
        raise ValueError("C1 settled-failure flattened lineage is malformed")
    expected_source = copy.deepcopy(recovery)
    expected_source.pop("c1_sources", None)
    expected_source["flattened_source_lineage"] = expected_lineage
    if source != expected_source:
        raise ValueError("C1 settled-failure source was not derived exactly")
    upstream_record = upstream[0]
    expected_upstream = {
        "source_index": migration.get("upstream_source_index"),
        "source_index_sha256": migration.get("upstream_source_index_sha256"),
        "source_run_id": migration.get("upstream_source_run_id"),
    }
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(key) != value for key, value in expected_upstream.items()
    ):
        raise ValueError("C1 settled-failure upstream provenance mismatch")
    if (
        migration.get("recovery_run_id") != recovery.get("run_id")
        or migration.get("recovery_git_commit") != recovery.get("git_commit")
        or migration.get("recovery_harness_hash") != recovery.get("harness_hash")
    ):
        raise ValueError("C1 settled-failure contract does not bind the recovery run")

    rows = [
        row
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    errors = [
        {
            "arm": arm,
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": row.get("task_id"),
            "source_status": "error",
        }
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    expected_rows, expected_ok, expected_pending = expected_coverage
    if (
        len(rows) != expected_rows
        or sum(row.get("status") == "ok" for row in rows) != expected_ok
        or sum(row.get("status") == "pending" for row in rows) != expected_pending
        or errors != expected_errors
        or any(row.get("status") == "quarantined_unknown_usage" for row in rows)
    ):
        raise ValueError("C1 settled-failure source coverage differs from registration")

    subprocess = __import__("subprocess")
    ast = __import__("ast")
    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 settled-failure migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 settled-failure migration cannot audit repository diff")
    allowed_paths = {
        "tests/test_c1_trial_persistence.py",
        "warptest/p5_aggregate.py",
        "warptest/p5_scale.py",
    }
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    if changed_paths != allowed_paths:
        raise ValueError(
            "C1 settled-failure migration contains unregistered changed paths: "
            + ", ".join(sorted(changed_paths ^ allowed_paths))
        )

    old_tree = ast.parse(git_file("warptest/p5_scale.py"))
    current_tree = ast.parse(
        (base / "warptest/p5_scale.py").read_text(encoding="utf-8")
    )
    ignored_functions = {
        "_validate_registered_c1_harness_migration",
        "_validate_registered_c1_recovery_resume_migration",
        "_validate_registered_c1_timeout_usage_snapshot_migration",
        "_validate_registered_c1_recursive_lineage_migration",
        "_validate_registered_c1_resume_settled_failure_migration",
    }
    old_tree.body = [
        node
        for node in old_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ignored_functions
        )
    ]
    current_tree.body = [
        node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ignored_functions
        )
    ]

    def orchestrator_class(tree: Any) -> Any:
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator":
                return node
        raise ValueError("C1 settled-failure orchestrator class is missing")

    old_class = orchestrator_class(old_tree)
    current_class = orchestrator_class(current_tree)
    old_methods = {
        child.name: child
        for child in old_class.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    current_class.body = [
        (
            old_methods[child.name]
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and child.name == "_completed"
            )
            else child
        )
        for child in current_class.body
    ]
    if ast.dump(current_tree) != ast.dump(old_tree):
        raise ValueError(
            "C1 settled-failure migration changed execution code outside resume "
            "evidence recognition"
        )


def _validate_registered_c1_retry_task_plan_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the source-specific retry-plan provenance repair migration."""

    affected_trials = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "jynew-save-restore",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "luanti-dig-node",
            "source_status": "error",
        },
    ]
    expected_profile = {
        "migration_id": "c1_retry_task_plan_resume_v1",
        "source_run_id": "p5-d65ab3c4aa7c",
        "source_git_commit": "7bdf4d095d6809c03e50f11f77954c58a734d958",
        "source_harness_hash": (
            "cf3fba87e98cc0a4513f7148162590f7e6df461a5454978ef808ef0a36c5e3ab"
        ),
        "affected_trials": affected_trials,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
    }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered retry-plan repair"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 retry-plan migration source provenance mismatch")

    errors = [
        {
            "arm": arm,
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": row.get("task_id"),
            "source_status": "error",
        }
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    rows = [
        row
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    if (
        len(rows) != 183
        or sum(row.get("status") == "ok" for row in rows) != 57
        or sum(row.get("status") == "pending" for row in rows) != 124
        or errors != affected_trials
        or any(row.get("status") == "quarantined_unknown_usage" for row in rows)
    ):
        raise ValueError("C1 retry-plan migration source coverage differs from 59/183")

    subprocess = __import__("subprocess")
    ast = __import__("ast")
    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 retry-plan migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 retry-plan migration cannot audit repository diff")
    allowed_paths = {
        "tests/test_c1_attempt_costs.py",
        "tests/test_c1_trial_persistence.py",
        "warptest/gui_agent.py",
        "warptest/p5_scale.py",
        "warptest/state_loop.py",
    }
    changed_paths = {line for line in changed.stdout.splitlines() if line}
    if changed_paths != allowed_paths:
        raise ValueError(
            "C1 retry-plan migration contains unregistered changed paths: "
            + ", ".join(sorted(changed_paths ^ allowed_paths))
        )
    if (
        migration.get("current_git_commit") != run_context.git_commit(base)
        or migration.get("changed_paths") != sorted(changed_paths)
    ):
        raise ValueError("C1 retry-plan migration does not bind the current diff")

    def tree_without_registered_changes(relative: str) -> tuple[Any, Any]:
        old_tree = ast.parse(git_file(relative))
        current_tree = ast.parse(
            (base / relative).read_text(encoding="utf-8")
        )
        return old_tree, current_tree

    for relative, function_name in (
        ("warptest/gui_agent.py", "run_gui_agent_benchmark"),
        ("warptest/state_loop.py", "run_state_loop_benchmark"),
    ):
        old_tree, current_tree = tree_without_registered_changes(relative)
        old_function = next(
            node
            for node in old_tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
        )
        current_tree.body = [
            old_function
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
            else node
            for node in current_tree.body
        ]
        if ast.dump(current_tree) != ast.dump(old_tree):
            raise ValueError(
                f"C1 retry-plan migration changed {relative} outside {function_name}"
            )

    old_tree, current_tree = tree_without_registered_changes("warptest/p5_scale.py")
    old_validator = next(
        node
        for node in old_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_validate_registered_c1_harness_migration"
    )
    current_tree.body = [
        old_validator
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_validate_registered_c1_harness_migration"
        else node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_validate_registered_c1_retry_task_plan_migration"
        )
    ]

    def orchestrator(tree: Any) -> Any:
        return next(
            node
            for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "P5Orchestrator"
        )

    old_class = orchestrator(old_tree)
    current_class = orchestrator(current_tree)
    old_methods = {
        node.name: node
        for node in old_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {
            "_import_c1_source_trials",
            "_validate_source_trial_artifacts",
        }
    }
    current_class.body = [
        old_methods[node.name]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in old_methods
        else node
        for node in current_class.body
    ]
    if ast.dump(current_tree) != ast.dump(old_tree):
        raise ValueError(
            "C1 retry-plan migration changed p5_scale outside registered recovery"
        )


def _validate_registered_c1_openra_render_grade_migration(
    *,
    base: Path,
    source: dict[str, Any],
    migration: dict[str, Any],
) -> None:
    """Validate the exact OpenRA paused-world read-only grader repair."""

    affected_trials = [
        {
            "arm": "c1_agent_s",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "openra-allied-combined-arms-rally",
            "source_status": "error",
        },
        {
            "arm": "c1_agent_s_synth",
            "model": "gpt-5.6-terra@relay",
            "seed": 0,
            "task_id": "openra-soviet-rocket-infantry-chain",
            "source_status": "error",
        },
    ]
    expected_profile = {
        "migration_id": "c1_openra_readonly_render_grade_v1",
        "source_run_id": "p5-e92822c0dfa1",
        "source_git_commit": "9b981e327ba001b422bc2381f68df3532ad841ce",
        "source_harness_hash": (
            "7dd8db55c5f367367d62d6cd636e87d512d0cf7401d8854692302c20480fa0a6"
        ),
        "affected_trials": affected_trials,
        "preserved_model_failure_trials": [],
        "completed_trial_import_only": True,
        "unfinished_tuples_requeued": True,
        "treatment_semantics_unchanged": True,
    }
    if any(migration.get(key) != value for key, value in expected_profile.items()):
        raise ValueError(
            "C1 harness migration does not match the registered OpenRA grader repair"
        )
    if (
        source.get("run_id") != expected_profile["source_run_id"]
        or source.get("git_commit") != expected_profile["source_git_commit"]
        or source.get("harness_hash") != expected_profile["source_harness_hash"]
    ):
        raise ValueError("C1 OpenRA grader migration source provenance mismatch")

    recovery_path = Path(str(migration.get("recovery_index") or "")).expanduser()
    if not recovery_path.is_file():
        raise ValueError("C1 OpenRA grader recovery index is missing")
    recovery_raw = recovery_path.read_bytes()
    recovery_sha = hashlib.sha256(recovery_raw).hexdigest()
    if recovery_sha != migration.get("recovery_index_sha256"):
        raise ValueError("C1 OpenRA grader recovery index SHA mismatch")
    try:
        recovery = json.loads(recovery_raw)
    except json.JSONDecodeError as error:
        raise ValueError("C1 OpenRA grader recovery index is unreadable") from error
    upstream = recovery.get("c1_sources") if isinstance(recovery, dict) else None
    if not isinstance(upstream, list) or len(upstream) != 1:
        raise ValueError("C1 OpenRA grader recovery must bind one upstream run")
    expected_lineage = {
        "schema_version": 1,
        "kind": "c1_flattened_recovery_source",
        "derived_from_index": str(recovery_path),
        "derived_from_index_sha256": recovery_sha,
        "upstream_sources": copy.deepcopy(upstream),
    }
    expected_source = copy.deepcopy(recovery)
    expected_source.pop("c1_sources", None)
    expected_source["flattened_source_lineage"] = expected_lineage
    if source != expected_source:
        raise ValueError("C1 OpenRA grader source was not derived exactly")
    upstream_record = upstream[0]
    expected_upstream = {
        "source_index": migration.get("upstream_source_index"),
        "source_index_sha256": migration.get("upstream_source_index_sha256"),
        "source_run_id": migration.get("upstream_source_run_id"),
    }
    if not isinstance(upstream_record, dict) or any(
        upstream_record.get(key) != value for key, value in expected_upstream.items()
    ):
        raise ValueError("C1 OpenRA grader upstream provenance mismatch")

    errors = [
        {
            "arm": arm,
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": row.get("task_id"),
            "source_status": "error",
        }
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict) and row.get("status") == "error"
    ]
    rows = [
        row
        for arm in C1_ARMS
        for row in (source.get("arms", {}).get(arm) or [])
        if isinstance(row, dict)
    ]
    if (
        len(rows) != 183
        or sum(row.get("status") == "ok" for row in rows) != 161
        or sum(row.get("status") == "pending" for row in rows) != 20
        or errors != affected_trials
        or any(row.get("status") == "quarantined_unknown_usage" for row in rows)
    ):
        raise ValueError("C1 OpenRA grader source coverage differs from 163/183")

    subprocess = __import__("subprocess")
    ast = __import__("ast")
    source_commit = str(source["git_commit"])

    def git_file(relative: str) -> str:
        completed = subprocess.run(
            ["git", "show", f"{source_commit}:{relative}"],
            cwd=str(base),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise ValueError(
                f"C1 OpenRA grader migration cannot read source file {relative}"
            )
        return completed.stdout

    changed = subprocess.run(
        ["git", "diff", "--name-only", source_commit, "--"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if changed.returncode != 0:
        raise ValueError("C1 OpenRA grader migration cannot audit repository diff")
    changed_paths = sorted(line for line in changed.stdout.splitlines() if line)
    expected_paths = [
        "benchmarks/p5/c1_tasks.yaml",
        "games/OpenRA",
        "tests/test_c1_openra_runtime.py",
        "tests/test_c1_v5_catalog.py",
        "warptest/p5_scale.py",
    ]
    changed_paths_sha256 = hashlib.sha256(
        json.dumps(changed_paths, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if (
        changed_paths != expected_paths
        or migration.get("current_git_commit") != run_context.git_commit(base)
        or migration.get("changed_paths") != changed_paths
        or migration.get("changed_paths_sha256") != changed_paths_sha256
    ):
        raise ValueError("C1 OpenRA grader migration does not bind the exact diff")

    old_child = subprocess.run(
        ["git", "rev-parse", f"{source_commit}:games/OpenRA"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    current_child = subprocess.run(
        ["git", "rev-parse", "HEAD:games/OpenRA"],
        cwd=str(base),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()
    child_changed = subprocess.run(
        ["git", "diff", "--name-only", old_child, current_child, "--"],
        cwd=str(base / "games" / "OpenRA"),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.splitlines()
    child_relative = "OpenRA.Mods.Common/Traits/World/WarptestGameplayProbe.cs"
    if child_changed != [child_relative]:
        raise ValueError("C1 OpenRA grader migration changed an unregistered child path")
    old_child_text = subprocess.run(
        ["git", "show", f"{old_child}:{child_relative}"],
        cwd=str(base / "games" / "OpenRA"),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    expected_child_text = old_child_text.replace(
        "public sealed class WarptestGameplayProbe : IWorldLoaded, ITick",
        "public sealed class WarptestGameplayProbe : IWorldLoaded, ITick, ITickRender",
        1,
    )
    render_method = """		void ITickRender.TickRender(WorldRenderer wr, Actor self)
		{
			if (!active || finished || !interactive || interactiveRequestActive)
				return;

			// Campaign scripts and the in-game menu may pause simulation ticks while
			// the rendered world remains available.  Formal GUI grading is read-only,
			// so consume an empty-action request here instead of timing out on the last
			// report sequence.  State-changing requests must still run through ITick.
			TryStartInteractiveRequest();
			if (interactiveRequestActive && actions.Count == 0)
				CompleteInteractiveRequest();
		}

"""
    marker = "\t\tvoid InitializeFuzzSessionWorld(World w)\n"
    expected_child_text = expected_child_text.replace(marker, render_method + marker, 1)
    current_child_text = (base / "games" / "OpenRA" / child_relative).read_text(
        encoding="utf-8"
    )
    if current_child_text != expected_child_text:
        raise ValueError("C1 OpenRA grader child diff is not the registered repair")

    revision_updates = {
        "benchmarks/p5/c1_tasks.yaml": (
            "revision: 8b8ebda1f56ec71c9a45bc4e6d63af910a24b11b",
            "revision: 7e687f430ee5b194c0ffaa06caaba757dd9155fb",
        ),
        "tests/test_c1_v5_catalog.py": (
            '"openra": "8b8ebda1f56ec71c9a45bc4e6d63af910a24b11b"',
            '"openra": "7e687f430ee5b194c0ffaa06caaba757dd9155fb"',
        ),
    }
    for relative, (old_revision, new_revision) in revision_updates.items():
        old_text = git_file(relative)
        expected_text = old_text.replace(old_revision, new_revision, 1)
        if (
            expected_text == old_text
            or (base / relative).read_text(encoding="utf-8") != expected_text
        ):
            raise ValueError(
                f"C1 OpenRA grader changed {relative} outside revision provenance"
            )

    old_p5 = git_file("warptest/p5_scale.py")
    old_import = """        harness_rebind = set(mismatches) == {"git_commit", "harness_hash"}
        if mismatches and not (
            harness_rebind and self.c1_source_contract_migration is not None
        ):
"""
    new_import = """        application_revisions_rebind = set(mismatches) == {
            "git_commit",
            "harness_hash",
            "task_manifest_hash",
            "c1_application_revisions",
        }
        harness_rebind = set(mismatches) == {"git_commit", "harness_hash"} or (
            application_revisions_rebind
        )
        if mismatches and not (
            harness_rebind and self.c1_source_contract_migration is not None
        ):
"""
    expected_p5 = old_p5.replace(old_import, new_import, 1)
    expected_p5 = expected_p5.replace(
        """        if harness_rebind:
            migration_needed.extend(["git_commit_rebind", "harness_rebind"])
""",
        """        if harness_rebind:
            migration_needed.extend(["git_commit_rebind", "harness_rebind"])
        if application_revisions_rebind:
            migration_needed.extend(
                ["c1_application_revisions_rebind", "task_manifest_hash_rebind"]
            )
""",
        1,
    )
    expected_p5 = expected_p5.replace(
        """            return {
                key: item
                for key, item in value.items()
                if key not in {"git_commit", "manifest_hash", "harness_hash"}
            }
""",
        """            migration_fields = {"git_commit", "manifest_hash", "harness_hash"}
            if application_revisions_rebind:
                migration_fields.add("c1_application_revisions")
            return {
                key: item
                for key, item in value.items()
                if key not in migration_fields
            }
""",
        1,
    )
    expected_tree = ast.parse(expected_p5)
    current_tree = ast.parse((base / "warptest/p5_scale.py").read_text(encoding="utf-8"))
    expected_validator = next(
        node
        for node in expected_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_validate_registered_c1_harness_migration"
    )
    current_tree.body = [
        expected_validator
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_validate_registered_c1_harness_migration"
        else node
        for node in current_tree.body
        if not (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "_validate_registered_c1_openra_render_grade_migration"
        )
    ]
    if ast.dump(current_tree) != ast.dump(expected_tree):
        raise ValueError("C1 OpenRA grader migration changed unregistered runtime code")


C1_TRIAL_PERSISTENCE_SCHEMA = 3
C1_OUTER_SCHEDULER_SCHEMA = 2
_C1_EXECUTION_IDENTITY_FIELDS = (
    "ordinal",
    "arm",
    "model",
    "seed",
    "model_index",
    "offset",
    "position",
    "ordered_arms",
)
BACKGROUND_PIXEL_PROTOCOL = "background_pixel_v1"
BACKGROUND_PIXEL_COORDINATE_SPACE = "frame_pixels_top_left_v1"
C1_START_GATE_PROTOCOL = {
    # v4 freezes one pair-level semantic/visual Start verdict before either E
    # transition and makes both arms consume it after local restore attestation.
    "schema_version": 4,
    "sequence": [
        "shared_pair_semantic_start_grade",
        "shared_pair_before_visual_grade_under_runtime_hold",
        "arm_local_restore_semantic_and_liveness_attestation",
        "phase_b_only_if_start_passed",
        "after_semantic_and_visual_grade",
    ],
    "arms_under_runtime_hold": [
        "c1_gui",
        "c1_gui_synth",
        "c1_agent_s",
        "c1_agent_s_synth",
        "c1_agent_scalecua",
        "c1_agent_scalecua_synth",
        "c1_state",
        "c1_state_minimal_agent",
    ],
    "shared_start_visual_rubric": {
        "default": "BEFORE_RUBRIC",
        "luanti": "LUANTI_BEFORE_RUBRIC",
    },
    "shared_start_spec": {
        "consumer": C1_SYNTHESIS_CONSUMER,
        "arms": list(C1_EXECUTION_SELF_SYNTHESIZING_ARMS),
        "acceptance": "c1_spec_acceptance.accept_c1_generated_spec",
    },
    "pair_start_gate": {
        "protocol": "c1-paired-start-gate-v1",
        "comparison_validity": "c1-minimal-core-outcome-validity-v1",
        "accepted_start_identity": "run_scoped_plain_id",
        "sha_policy": "optional_diagnostic_never_comparison_gate",
        "cache": "run_scoped_immutable_before_transition",
        "before_visual_judge_max_calls_per_pair": 1,
        "local_attestation": [
            "restore_success",
            "hidden_semantic_matches_pair",
            "registered_capture_source",
            "informative_screenshot",
            "runtime_healthy",
        ],
        "local_drift_status": "retryable_infrastructure",
    },
    "goal_evaluation_status": {
        "passed": "evaluated",
        "semantic_failed": "not_evaluated_start_failed",
        "visual_failed": "not_evaluated_start_failed",
        "visual_incomplete": "not_evaluated_start_failed",
    },
    "short_circuit": {
        "semantic_failed": "do_not_call_before_visual_or_phase_b_or_after",
        "before_visual_incorrect": "do_not_call_phase_b_or_after",
        "before_visual_incomplete": "do_not_call_phase_b_or_after",
    },
    "runtime_hold_backend": {
        "browser": "chromium_cdp_script_pause",
        "luanti": "sigstop",
        "native_desktop": "sigstop",
        # The state arm's Unity path grades after the adapter is closed, so the
        # start cannot drift and there is nothing left to suspend.
        "state_unity_terminated": "runtime_terminated_before_grading",
    },
    "synthesized_prestart_fair_zero": {
        "allowed_start_construction_status": [
            "synthesis_failed",
            "restore_failed",
        ],
        "requires": [
            "incomplete_false",
            "policy_not_started",
            "start_grading_not_started",
            "visual_grading_not_started",
            "zero_steps",
            "goal_not_evaluated",
        ],
        "start_gate_status": "not_evaluated_start_construction_failed",
        "goal_evaluation_status": "not_evaluated_start_construction_failed",
        "by_status": {
            "synthesis_failed": [
                "runtime_not_started",
                "seed_unobserved",
                "prng_absent",
                "checkpoint_restore_zero",
            ],
            "restore_failed": [
                "runtime_started",
                "seed_observed",
                "prng_observed",
                "restore_time_recorded",
            ],
        },
    },
}


class IncompleteC1InvocationError(RuntimeError):
    """Retryable signal: a producer persisted partial data after infra failure."""


class NonRetryableC1InvocationError(RuntimeError):
    """A partial C1 report requires diagnosis, not stochastic replay."""


class QuarantinedC1TrialsError(RuntimeError):
    """One or more trial-atomic C1 tuples are incomplete and quarantined."""


def _c1_trial_persistence_policy(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the registered trial-atomic policy, rejecting ambiguous variants.

    The opt-in keeps old ad-hoc manifests usable for offline unit tests, while
    the formal manifest is fail-closed on one exact schema.  In particular,
    legacy invocation-level indexes cannot be imported as trial evidence.
    """

    raw = manifest.get("c1_trial_persistence")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("c1_trial_persistence must be a mapping")
    expected = {
        "schema_version": C1_TRIAL_PERSISTENCE_SCHEMA,
        "atomic_unit": "trial",
        "unknown_usage": "quarantine_primary_attempt",
        "same_attempt_resume": "forbidden",
        "source_import": "all_completed_tuples",
        "replacement_selection": "all_noncompleted_tuples",
        "legacy_import": "forbidden",
        "normal_execution": "shared_invocation_incremental_snapshot_v1",
        "recovery_execution": "one_task_fresh_invocation_v1",
        "recovery_unknown_resume": "fresh_attempt_same_clean_root",
        "replaceable_source_errors": [
            "exact_usage_task_timeout",
            "exact_usage_retryable_infrastructure",
        ],
        "max_recovery_attempts_per_tuple": 3,
        "attempt_order": "deterministic_registered_tuple_order",
        "global_unknown_stop": True,
    }
    mismatches = [key for key, value in expected.items() if raw.get(key) != value]
    if mismatches:
        raise ValueError(
            "c1_trial_persistence does not match the registered policy: "
            + ", ".join(mismatches)
        )
    return copy.deepcopy(expected)


def _usage_accounting_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("usage_accounting")
    expected = {
        "schema": "c1-usage-accounting-v2",
        "required_primary_fields": [
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
        ],
        "primary_total_equation": "total_tokens=prompt_tokens+completion_tokens",
        "reasoning_detail": {
            "missing_policy": "bounded_completion_token_mass",
            "tolerance_fraction": cost.REASONING_DETAIL_TOLERANCE_FRACTION,
            "scope": "provider_canonical_model",
            "runtime_action": "continue",
            "imputation": "forbidden",
        },
    }
    if raw is None:
        return copy.deepcopy(expected)
    if raw != expected:
        raise ValueError("usage_accounting does not match c1-usage-accounting-v2")
    return copy.deepcopy(expected)


def _ordered_c1_task_ids(
    manifest: dict[str, Any], task_ids: list[str] | None
) -> list[str] | None:
    """Apply the registered Luanti-first order only to the exact shakeout set."""

    if not task_ids:
        registered_default = manifest.get("c1_task_ids")
        if registered_default is None:
            return None
        if not isinstance(registered_default, list) or not registered_default:
            raise ValueError("c1_task_ids must be a non-empty list when declared")
        task_ids = [str(task_id) for task_id in registered_default]
    ordered = list(dict.fromkeys(str(task_id) for task_id in task_ids if str(task_id)))
    if len(ordered) != len(task_ids):
        raise ValueError("C1 task selection must contain unique non-empty task ids")
    shakeout = manifest.get("shakeout")
    if not isinstance(shakeout, dict):
        return ordered
    registered = shakeout.get("task_ids")
    execution = shakeout.get("execution_order")
    if not isinstance(registered, list) or not isinstance(execution, list):
        return ordered
    registered_ids = [str(value) for value in registered]
    execution_ids = [str(value) for value in execution]
    if (
        len(registered_ids) != len(set(registered_ids))
        or len(execution_ids) != len(set(execution_ids))
        or set(registered_ids) != set(execution_ids)
    ):
        raise ValueError("shakeout task_ids and execution_order must be the same unique set")
    return execution_ids if set(ordered) == set(registered_ids) and len(ordered) == len(registered_ids) else ordered


def _c1_task_manifest_hash(
    *,
    base: Path,
    manifest: dict[str, Any],
    arms: list[str],
    task_ids: list[str] | None,
    manifest_hash: str,
) -> str:
    """Bind the complete catalog bytes plus the selected task rows."""

    selected_arms = [arm for arm in arms if arm in C1_EXECUTION_ARMS]
    if not selected_arms:
        return (
            run_context.manifest_hash(
                {"manifest_hash": manifest_hash, "task_ids": task_ids}
            )
            if task_ids
            else manifest_hash
        )
    arm_cfg = manifest.get("arms", {}).get(selected_arms[0], {}) or {}
    raw_path = arm_cfg.get("tasks", "benchmarks/p5/c1_tasks.yaml")
    task_path = Path(str(raw_path)).expanduser()
    if not task_path.is_absolute():
        task_path = (base / task_path).resolve()
    if not task_path.is_file():
        # A few isolated scheduling tests intentionally replace every producer
        # before creating a catalog. A real invocation loads this path and fails.
        return run_context.manifest_hash(
            {"manifest_hash": manifest_hash, "task_ids": task_ids}
        )
    raw = task_path.read_bytes()
    payload = yaml.safe_load(raw)
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    if not isinstance(tasks, list) or not all(isinstance(task, dict) for task in tasks):
        raise ValueError(f"C1 task file must contain mapping task rows: {task_path}")
    if task_ids:
        by_id = {str(task.get("id") or ""): task for task in tasks}
        missing = [task_id for task_id in task_ids if task_id not in by_id]
        if missing:
            raise ValueError("Unknown C1 task id(s): " + ", ".join(missing))
        selected_rows = [by_id[task_id] for task_id in task_ids]
    else:
        selected_rows = tasks
    try:
        task_file = task_path.relative_to(base).as_posix()
    except ValueError:
        task_file = str(task_path)
    return run_context.manifest_hash(
        {
            "manifest_hash": manifest_hash,
            "task_file": task_file,
            "task_file_sha256": hashlib.sha256(raw).hexdigest(),
            "task_file_content": raw.decode("utf-8"),
            "benchmark_hosts": (
                payload.get("benchmark_hosts") if isinstance(payload, dict) else None
            ),
            "selected_tasks": selected_rows,
        }
    )


def _c1_start_gate_protocol(manifest: dict[str, Any]) -> dict[str, Any] | None:
    raw = manifest.get("c1_start_gate_protocol")
    if raw is None:
        return None
    if raw != C1_START_GATE_PROTOCOL:
        raise ValueError("c1_start_gate_protocol does not match the registered protocol")
    return copy.deepcopy(C1_START_GATE_PROTOCOL)


def _runtime_lock_policy(manifest: dict[str, Any]) -> dict[str, Any]:
    """Resolve and validate the manifest-registered machine lock namespace.

    The resolved directory is scientific run provenance: changing it can remove
    cross-process exclusion while leaving every task/model setting unchanged.  A
    launcher may set ``WARPTEST_RUNTIME_LOCK_DIR``, but only as an equivalent
    spelling of the registered path (for example ``/tmp`` vs ``/private/tmp`` on
    macOS), never as an unregistered treatment change.
    """

    configured = manifest.get("runtime_lock")
    if configured is None:
        configured = {}
    if not isinstance(configured, dict):
        raise ValueError("runtime_lock must be a mapping")
    scope = str(configured.get("scope") or DEFAULT_RUNTIME_LOCK_SCOPE)
    if scope != DEFAULT_RUNTIME_LOCK_SCOPE:
        raise ValueError(
            f"runtime_lock.scope must be {DEFAULT_RUNTIME_LOCK_SCOPE!r}"
        )
    raw_dir = configured.get("lock_dir", str(DEFAULT_RUNTIME_LOCK_DIR))
    if not isinstance(raw_dir, str) or not raw_dir.strip():
        raise ValueError("runtime_lock.lock_dir must be a non-empty absolute path")
    registered = Path(raw_dir).expanduser()
    if not registered.is_absolute():
        raise ValueError("runtime_lock.lock_dir must be an absolute path")
    resolved = registered.resolve()
    override = os.environ.get(RUNTIME_LOCK_DIR_ENV)
    if override and Path(override).expanduser().resolve() != resolved:
        raise RuntimeError(
            f"{RUNTIME_LOCK_DIR_ENV} does not match the manifest-registered "
            "runtime_lock.lock_dir"
        )
    wait_timeout_s = validate_runtime_lock_wait_timeout_s(
        configured.get(
            "wait_timeout_s", DEFAULT_RUNTIME_LOCK_WAIT_TIMEOUT_S
        )
    )
    wait_override = os.environ.get(RUNTIME_LOCK_WAIT_TIMEOUT_ENV)
    if wait_override is not None:
        try:
            override_timeout_s = validate_runtime_lock_wait_timeout_s(
                float(wait_override)
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"{RUNTIME_LOCK_WAIT_TIMEOUT_ENV} must be a positive finite number"
            ) from error
        if override_timeout_s != wait_timeout_s:
            raise RuntimeError(
                f"{RUNTIME_LOCK_WAIT_TIMEOUT_ENV} does not match the "
                "manifest-registered runtime_lock.wait_timeout_s"
            )
    return {
        "scope": scope,
        "lock_dir": str(resolved),
        "wait_timeout_s": wait_timeout_s,
        "task_clock_policy": "starts_after_resource_acquisition",
    }


def require_complete_c3_fuzz_result(result: dict[str, Any]) -> dict[str, Any]:
    """Reject a partial fuzz report before P5 can record it as a zero-defect arm.

    ``run_fuzz_benchmark`` intentionally persists task-level failures for resume
    and diagnosis. That is useful for an interactive fuzz run, but P5 results are
    publishable aggregates: a failed OpenRA warm session must invalidate the C3
    invocation instead of looking like a clean run with no defects.
    """

    raw_results = result.get("results")
    if not isinstance(raw_results, list) or not raw_results:
        raise ValueError("C3 fuzz invocation incomplete: no task results were produced")

    failures: list[str] = []
    for task in raw_results:
        if not isinstance(task, dict):
            failures.append("malformed task result")
            continue
        status = task.get("status")
        if status == "success":
            continue
        task_id = str(task.get("task_id") or "<unknown-task>")
        reason = str(task.get("reason") or status or "missing task status")
        failures.append(f"{task_id}: {reason}")
    if failures:
        raise ValueError("C3 fuzz invocation incomplete: " + "; ".join(failures[:8]))
    return result


def load_manifest(path: str | Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "arms" not in data:
        raise ValueError(f"P5 manifest must be a mapping with an 'arms' block: {path}")
    return data


def cost_env_value(manifest: dict[str, Any]) -> str | None:
    """JSON string for WARPTEST_LLM_USD_PER_MTOK_BY_MODEL from the manifest."""

    if manifest.get("cost", {}).get("verified") is not True:
        return None
    table = manifest.get("cost", {}).get("usd_per_mtok_by_model")
    if not isinstance(table, dict) or not table:
        return None
    return json.dumps(table, ensure_ascii=False)


def visual_judge_env_values(manifest: dict[str, Any]) -> dict[str, str]:
    """Environment values for the visual judge, derived from the P5 manifest.

    The API key itself is copied only from the configured environment variable at
    runtime. It is never read from or written to the manifest.
    """

    api_cfg = manifest.get("api", {}) if isinstance(manifest.get("api"), dict) else {}
    judge_cfg = manifest.get("visual_judge", {}) if isinstance(manifest.get("visual_judge"), dict) else {}
    key_env = str(judge_cfg.get("key_env") or api_cfg.get("key_env") or "")
    values: dict[str, str] = {}
    if key_env and os.environ.get(key_env):
        values["WARPTEST_VISUAL_JUDGE_API_KEY"] = os.environ[key_env]
    base_url_env = judge_cfg.get("base_url_env") or api_cfg.get("base_url_env")
    base_url = (os.environ.get(str(base_url_env)) if base_url_env else None) or judge_cfg.get("base_url") or api_cfg.get("base_url")
    if base_url:
        values["WARPTEST_VISUAL_JUDGE_BASE_URL"] = str(base_url)
    model = judge_cfg.get("model") or manifest.get("visual_judge_model")
    if model:
        values["WARPTEST_VISUAL_JUDGE_MODEL"] = str(model)
    return values


@contextlib.contextmanager
def _manifest_visual_judge_env(manifest: dict[str, Any]):
    """Temporarily force visual-judge routing to the manifest values."""

    values = visual_judge_env_values(manifest)
    patched: dict[str, str | None] = {}
    for key, value in values.items():
        patched[key] = os.environ.get(key)
        os.environ[key] = value
    try:
        yield
    finally:
        for key, previous in patched.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous


def write_subset_tasks(
    base: Path,
    full_tasks_path: str | Path,
    subset_ids: list[str],
    out_path: Path,
) -> Path:
    """Write a filtered state-loop task file containing only ``subset_ids``."""

    path = Path(full_tasks_path)
    if not path.is_absolute():
        path = (base / path).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError(f"C1 task file must contain a tasks list: {path}")
    # Validate the complete registered source before emitting a derived catalog.
    # The derived file may contain too few tasks to satisfy catalog-wide analysis
    # coverage thresholds, but it must never let a malformed source bypass them.
    load_state_loop_tasks(base, path)
    by_id = {
        str(task.get("id")): task
        for task in data["tasks"]
        if isinstance(task, dict) and task.get("id")
    }
    missing = [task_id for task_id in subset_ids if task_id not in by_id]
    if missing:
        raise ValueError("Unknown C1 task id(s): " + ", ".join(missing))
    tasks = [by_id[task_id] for task_id in subset_ids]
    data = {
        **data,
        "tasks": tasks,
        "source_task_file": str(path),
        "selected_task_ids": list(subset_ids),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = out_path.with_name(out_path.name + ".tmp")
    temporary.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(temporary, out_path)
    return out_path


def _patch_retry_history(report_dir: str | Path | None, filename: str, history: list[dict[str, Any]]) -> None:
    """Re-stamp the producer's report with the orchestrator's full retry history."""

    if not report_dir:
        return
    report_path = Path(report_dir) / filename
    if not report_path.exists():
        return
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return
    payload.setdefault("run_context", {})["retry_history"] = history
    write_json(report_path, payload)


_UNSET_C2_REPORT_SHA256 = object()


def _c2_memory_report_issue(
    report_path: Path, *, expected_sha256: Any = _UNSET_C2_REPORT_SHA256
) -> str | None:
    """Return why a persisted C2 suite is not formal evidence, if anything."""

    try:
        report_raw = report_path.read_bytes()
        payload = json.loads(report_raw)
    except (OSError, json.JSONDecodeError, TypeError) as error:
        return f"C2 memory report is unreadable: {run_context.safe_error_text(error)}"
    if not isinstance(payload, dict):
        return "C2 memory report must be a JSON object"
    if expected_sha256 is not _UNSET_C2_REPORT_SHA256:
        if (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256.lower())
        ):
            return "C2 memory report index has an invalid report_sha256"
        actual_sha256 = hashlib.sha256(report_raw).hexdigest()
        if actual_sha256 != expected_sha256.lower():
            return "C2 memory report_sha256 does not match the persisted report"
    formal_validity = payload.get("formal_validity")
    if (
        not isinstance(formal_validity, dict)
        or formal_validity.get("status") != "valid"
    ):
        return "C2 memory report formal_validity.status is not valid"
    return None


class P5Orchestrator:
    def __init__(
        self,
        root: str | Path,
        manifest: dict[str, Any],
        *,
        run_dir: Path,
        models: list[str] | None = None,
        seeds: int | None = None,
        arms: list[str] | None = None,
        agent_kind: str = "llm",
        headless: bool = True,
        fake_visual: bool = False,
        memory_workers: int = 1,
        c1_outer_worker: int = 8,
        c1_inner_worker: int = 4,
        resume: bool = False,
        preflight_artifact: Path | None = None,
        calibration_artifact: Path | None = None,
        task_ids: list[str] | None = None,
        c1_source_index: Path | None = None,
        c1_source_contract_migration: Path | None = None,
    ) -> None:
        self.base = Path(root).expanduser().resolve()
        self.manifest = manifest
        self.runtime_lock_policy = _runtime_lock_policy(manifest)
        self.run_dir = run_dir
        self.models = models or list(manifest.get("models", []))
        self.seeds = seeds if seeds is not None else int(manifest.get("seeds", 1))
        self.arms = arms or list(DEFAULT_ARMS)
        self.agent_kind = agent_kind
        self.headless = headless
        self.fake_visual = fake_visual
        self.resume = bool(resume)
        self.task_ids = _ordered_c1_task_ids(manifest, task_ids)
        self._selected_tasks_path: Path | None = None
        self._trial_task_paths: dict[str, Path] = {}
        self._trial_task_paths_lock = threading.Lock()
        self.usage_accounting = _usage_accounting_policy(manifest)
        self.c1_trial_persistence = _c1_trial_persistence_policy(manifest)
        self.c1_start_gate_protocol = _c1_start_gate_protocol(manifest)
        self.c1_source_index = (
            Path(c1_source_index).expanduser().resolve()
            if c1_source_index is not None
            else None
        )
        self.c1_source_contract_migration = (
            Path(c1_source_contract_migration).expanduser().resolve()
            if c1_source_contract_migration is not None
            else None
        )
        if (
            self.c1_source_contract_migration is not None
            and self.c1_source_index is None
        ):
            raise ValueError(
                "--c1-source-contract-migration requires --c1-source-index"
            )
        if self.c1_source_index is not None and self.c1_trial_persistence is None:
            raise ValueError(
                "--c1-source-index requires the registered trial-atomic C1 schema"
            )
        self.c1_sources: list[dict[str, Any]] = []
        self.c1_source_contract_migration_metadata: dict[str, Any] | None = None
        self.c1_operational_overhead: list[dict[str, Any]] = []
        self._c1_replacement_sources: dict[TrialKey, dict[str, Any]] = {}
        self._c1_source_imported = False
        self._c1_quarantined: list[dict[str, Any]] = []
        self.preflight_artifact = preflight_artifact
        self._preflight_gate_checked = False
        self.preflight_metadata: dict[str, Any] | None = None
        self.calibration_artifact = calibration_artifact
        self._calibration_gate_checked = False
        self.calibration_metadata: dict[str, Any] | None = None
        self.execution_order: list[dict[str, Any]] = []
        self.latin_square: dict[str, Any] = {
            "base_order": list(C1_ARMS),
            "offset_rule": f"(model_index * seeds + seed) mod {len(C1_ARMS)}",
            "blocks": [],
        }
        self._latin_local = threading.local()
        self._c1_global_stop_event: C1GlobalStop | threading.Event | None = None
        if memory_workers < 1:
            raise ValueError("memory_workers must be at least 1")
        self.memory_workers = memory_workers
        if c1_outer_worker < 1:
            raise ValueError("c1_outer_worker must be at least 1")
        if c1_inner_worker < 1:
            raise ValueError("c1_inner_worker must be at least 1")
        self.c1_outer_worker = int(c1_outer_worker)
        self.c1_inner_worker = int(c1_inner_worker)
        self.c1_sandbox_manager: C1SandboxManager | None = None
        self.c1_sandbox_preflight: dict[str, Any] | None = None
        self.c1_unity_canary: dict[str, Any] | None = None
        self.c1_unity_serialized = False
        self.c1_unity_serialized_benchmarks: list[str] = []
        self.c1_outer_scheduler: dict[str, Any] = {
            "schema_version": C1_OUTER_SCHEDULER_SCHEMA,
            "requested_outer_workers": self.c1_outer_worker,
            "effective_outer_workers": 0,
            "max_active_invocations": 0,
        }
        self._outer_active = 0
        self._outer_started = 0
        self._outer_completed = 0
        api_cfg = manifest.get("api", {}) if isinstance(manifest.get("api"), dict) else {}
        self.api_key_env = api_cfg.get("key_env", "MODELSCOPE_API_KEY")
        base_url_env = api_cfg.get("base_url_env")
        self.base_url = (
            os.environ.get(str(base_url_env)) if base_url_env else None
        ) or api_cfg.get("base_url", "https://api-inference.modelscope.cn/v1")
        self.temperature = float(api_cfg.get("temperature", 0.2))
        self.provider_timeout_registered = "provider_timeout_s" in api_cfg
        self.provider_timeout_s = (
            manifest_provider_timeout_s(manifest)
            if self.provider_timeout_registered
            else C1_PROVIDER_TIMEOUT_S
        )
        self.c1_task_timeout_s = validate_task_timeout_s(
            manifest.get("c1_task_timeout_s", DEFAULT_C1_TASK_TIMEOUT_S)
        )
        self.c1_ready_scheduler = C1ReadyScheduler(
            wait_timeout_s=float(self.runtime_lock_policy["wait_timeout_s"])
        )
        raw_profiles = manifest.get("model_request_profiles")
        if raw_profiles is None:
            raw_profiles = {}
        if not isinstance(raw_profiles, dict):
            raise ValueError("model_request_profiles must be a model-to-profile mapping")
        canonical_profiles = canonical_profiles_json(raw_profiles)
        self.model_request_profiles: dict[str, str] = json.loads(canonical_profiles)
        self.model_request_profiles_sha256 = hashlib.sha256(
            canonical_profiles.encode("utf-8")
        ).hexdigest()
        raw_background_protocol = manifest.get("c1_background_pixel_protocol")
        self.c1_background_pixel_protocol: dict[str, Any] | None = None
        self.c1_background_pixel_protocol_sha256: str | None = None
        _agent_s_arms_selected = any(
            arm in {"c1_agent_s", "c1_agent_s_synth"} for arm in self.arms
        )
        _scalecua_arms_selected = any(
            arm in {"c1_agent_scalecua", "c1_agent_scalecua_synth"}
            for arm in self.arms
        )
        if _agent_s_arms_selected or _scalecua_arms_selected:
            if not isinstance(raw_background_protocol, dict):
                raise ValueError("formal C/D require c1_background_pixel_protocol")
            required_protocol = {
                "schema_version": 1,
                "protocol": "warptest-agent-s-v2",
                "execution_backend": BACKGROUND_PIXEL_PROTOCOL,
                "foreground_required": False,
                "coordinate_space": BACKGROUND_PIXEL_COORDINATE_SPACE,
                "fail_closed": True,
                "allow_foreground_fallback": False,
            }
            if not _agent_s_arms_selected:
                # A pure-ScaleCUA run does not execute the Agent-S treatment,
                # so its protocol id is not part of this run's identity.
                required_protocol.pop("protocol")
            mismatches = [
                key
                for key, expected in required_protocol.items()
                if raw_background_protocol.get(key) != expected
            ]
            if _scalecua_arms_selected and raw_background_protocol.get(
                "scalecua_protocol"
            ) != "warptest-scalecua-v1":
                mismatches.append("scalecua_protocol")
            benchmarks = raw_background_protocol.get("benchmarks")
            if mismatches or not isinstance(benchmarks, dict) or len(benchmarks) != 10:
                detail = ", ".join(mismatches) or "benchmarks"
                raise ValueError(
                    "c1_background_pixel_protocol does not match the formal protocol: "
                    + detail
                )
            canonical_background = json.dumps(
                raw_background_protocol,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
            self.c1_background_pixel_protocol = copy.deepcopy(raw_background_protocol)
            self.c1_background_pixel_protocol_sha256 = hashlib.sha256(
                canonical_background.encode("utf-8")
            ).hexdigest()
        self.manifest_hash = run_context.manifest_hash(manifest)
        self.task_manifest_hash = _c1_task_manifest_hash(
            base=self.base,
            manifest=self.manifest,
            arms=self.arms,
            task_ids=self.task_ids,
            manifest_hash=self.manifest_hash,
        )
        self.run_id = run_context.new_run_id("p5")
        self.git_commit = run_context.git_commit(self.base)
        self.harness_hash: str | None = None
        self.c1_application_revisions: dict[str, dict[str, str]] | None = None
        self.c1_environment: dict[str, Any] | None = None
        self.c2_harness_hash: str | None = (
            c2_harness_hash(self.base)
            if "c2_memory_baseline" in self.arms
            else None
        )
        # A lightweight, filesystem-free counterpart to c2_harness_hash: it
        # covers only the manifest fields the C2 arm reads (see
        # c2_provenance.c2_manifest_fingerprint), so a C2-only resume or the
        # aggregator can detect C2 config drift without recomputing the full
        # runtime/gitlink harness hash.
        self.c2_manifest_fingerprint: str | None = (
            c2_manifest_fingerprint(self.manifest)
            if "c2_memory_baseline" in self.arms
            else None
        )
        retry = manifest.get("retry", {})
        self.max_attempts = int(retry.get("max_attempts", 6))
        self.base_sleep_s = float(retry.get("base_sleep_s", 5.0))
        self.max_sleep_s = float(retry.get("max_sleep_s", 90.0))
        self.index: dict[str, list[dict[str, Any]]] = {arm: [] for arm in self.arms}
        self._index_lock = threading.Lock()
        self._resume_index_loaded = False
        if (
            any(arm in C1_EXECUTION_ARMS for arm in self.arms)
            and isinstance(self.manifest.get("c1_reproducibility"), dict)
        ):
            self._freeze_c1_reproducibility()
        self._load_resume_index()

    # -- helpers ----------------------------------------------------------------

    @property
    def _index_path(self) -> Path:
        return self.run_dir / "p5-run-index.json"

    def _load_resume_index(self) -> None:
        if not self.resume or not self._index_path.exists():
            return
        previous = json.loads(self._index_path.read_text(encoding="utf-8"))
        self._resume_index_loaded = True
        # A C2-only run shares this manifest file with C1/C3, but must not be
        # invalidated by a concurrent, unrelated edit to those arms (e.g.
        # c3_fuzz mutation tuning). Its own config/code drift is instead
        # checked below via c2_manifest_fingerprint / c2_harness_hash.
        c2_only = self.arms == ["c2_memory_baseline"]
        expected = {
            "models": self.models,
            "seeds": self.seeds,
            "task_ids": self.task_ids,
            "arms_run": self.arms,
            "agent_kind": self.agent_kind,
            "headless": self.headless,
            "c1_outer_worker": self.c1_outer_worker,
            "c1_inner_worker": self.c1_inner_worker,
            "c1_task_timeout_s": self.c1_task_timeout_s,
            "model_request_profiles": self.model_request_profiles,
            "model_request_profiles_sha256": self.model_request_profiles_sha256,
            "runtime_lock": self.runtime_lock_policy,
            "usage_accounting": self.usage_accounting,
            "c1_trial_persistence": self.c1_trial_persistence,
            "c1_start_gate_protocol": self.c1_start_gate_protocol,
            "c1_background_pixel_protocol": self.c1_background_pixel_protocol,
            "c1_background_pixel_protocol_sha256": self.c1_background_pixel_protocol_sha256,
            "c1_source_index_request": (
                str(self.c1_source_index) if self.c1_source_index else None
            ),
            "c1_source_contract_migration_request": (
                str(self.c1_source_contract_migration)
                if self.c1_source_contract_migration
                else None
            ),
        }
        if not c2_only:
            expected["manifest_hash"] = self.manifest_hash
            expected["task_manifest_hash"] = self.task_manifest_hash
        if any(arm in C1_EXECUTION_ARMS for arm in self.arms):
            expected["provider_timeout_s"] = self.provider_timeout_s
            expected["c1_application_revisions"] = self.c1_application_revisions
            expected["c1_environment"] = self.c1_environment
            expected["c1_prompt_revisions"] = (
                self._c1_prompt_revision_provenance()
            )
            expected["c1_random_controls"] = (
                self._c1_random_control_provenance()
            )
            if previous.get("c1_resource_scheduler") is None:
                execution_rows = previous.get("execution_order") or []
                incomplete_old_root = any(
                    isinstance(row, dict)
                    and row.get("status") not in {
                        "complete",
                        "imported_complete",
                    }
                    for row in execution_rows
                )
                if incomplete_old_root:
                    raise ValueError(
                        "Cannot resume an incomplete pre-ready-scheduler C1 root "
                        "in place; use a clean root with --c1-source-index and an "
                        "explicit scheduler-only contract migration artifact"
                    )
        mismatches = [key for key, value in expected.items() if previous.get(key) != value]
        if mismatches:
            raise ValueError(
                "Cannot resume P5 run with changed provenance: "
                + ", ".join(mismatches)
            )
        previous_commit = previous.get("git_commit")
        if previous_commit and previous_commit != self.git_commit:
            raise ValueError("Cannot resume P5 run with a different git commit")
        if "c2_memory_baseline" in self.arms:
            previous_c2_harness = previous.get("c2_harness_hash")
            if not isinstance(previous_c2_harness, str) or not previous_c2_harness:
                raise ValueError(
                    "Cannot resume C2 run without a C2 harness hash; use a new report directory"
                )
            if c2_only:
                previous_fingerprint = previous.get("c2_manifest_fingerprint")
                if not isinstance(previous_fingerprint, str) or not previous_fingerprint:
                    raise ValueError(
                        "Cannot resume a C2-only run recorded before c2_manifest_fingerprint "
                        "was tracked; use a new report directory"
                    )
                if previous_fingerprint != self.c2_manifest_fingerprint:
                    raise ValueError(
                        "Cannot resume P5 after the C2 manifest config changed; use a new report directory"
                    )
            if previous_c2_harness != self._freeze_c2_harness_hash():
                raise ValueError(
                    "Cannot resume P5 after the C2 harness changed; use a new report directory"
                )
        self.run_id = str(previous.get("run_id") or self.run_id)
        if isinstance(previous.get("calibration"), dict):
            self.calibration_metadata = dict(previous["calibration"])
        if isinstance(previous.get("preflight"), dict):
            self.preflight_metadata = dict(previous["preflight"])
        previous_harness = previous.get("harness_hash") or (
            self.calibration_metadata.get("harness_hash")
            if isinstance(self.calibration_metadata, dict)
            else None
        )
        if previous_harness and any(arm in C1_EXECUTION_ARMS for arm in self.arms):
            current_harness = c1_harness_hash(self.base)
            if previous_harness != current_harness:
                raise ValueError(
                    "Cannot resume P5 run after the C1 harness changed; use a new report directory"
                )
            self.harness_hash = str(previous_harness)
        for arm in self.arms:
            rows = previous.get("arms", {}).get(arm, [])
            if isinstance(rows, list):
                self.index[arm] = [dict(row) for row in rows if isinstance(row, dict)]
        self.c1_sources = [
            dict(row)
            for row in (previous.get("c1_sources") or [])
            if isinstance(row, dict)
        ]
        previous_migration = previous.get("c1_source_contract_migration")
        self.c1_source_contract_migration_metadata = (
            dict(previous_migration)
            if isinstance(previous_migration, dict)
            else None
        )
        self.c1_operational_overhead = [
            dict(row)
            for row in (previous.get("c1_operational_overhead") or [])
            if isinstance(row, dict)
        ]
        if any(arm in C1_EXECUTION_ARMS for arm in self.arms):
            resource_scheduler = previous.get("c1_resource_scheduler")
            if isinstance(resource_scheduler, dict):
                self.c1_ready_scheduler.restore_provenance(
                    resource_scheduler
                )
            self._restore_c1_scheduler_provenance(previous)
        # A recovery resume must re-read and re-hash the original source before
        # any remaining paid tuple. Import is idempotent and rebuilds the exact
        # replacement map below.
        self._c1_source_imported = self.c1_source_index is None

    @staticmethod
    def _execution_identity(row: dict[str, Any]) -> dict[str, Any]:
        return {field: copy.deepcopy(row.get(field)) for field in _C1_EXECUTION_IDENTITY_FIELDS}

    @staticmethod
    def _legacy_dispatch_history(row: dict[str, Any]) -> list[dict[str, Any]]:
        """Losslessly wrap schema-v1 outer timing without inventing evidence."""

        start = row.get("start_ordinal")
        started_ns = row.get("started_monotonic_ns")
        if start is None and started_ns is None:
            return []
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or start < 0
            or not isinstance(started_ns, int)
            or isinstance(started_ns, bool)
            or started_ns < 0
        ):
            raise ValueError(
                "Cannot resume C1 scheduler schema v1 with incomplete start provenance"
            )
        dispatch: dict[str, Any] = {
            "dispatch_ordinal": 0,
            "start_ordinal": start,
            "started_monotonic_ns": started_ns,
            "status": str(row.get("status") or "interrupted"),
        }
        completion = row.get("completion_ordinal")
        completed_ns = row.get("completed_monotonic_ns")
        if completion is not None or completed_ns is not None:
            if (
                not isinstance(completion, int)
                or isinstance(completion, bool)
                or completion < 0
                or not isinstance(completed_ns, int)
                or isinstance(completed_ns, bool)
                or completed_ns < 0
            ):
                raise ValueError(
                    "Cannot resume C1 scheduler schema v1 with incomplete completion provenance"
                )
            dispatch.update(
                completion_ordinal=completion,
                completed_monotonic_ns=completed_ns,
            )
        elif dispatch["status"] == "running":
            dispatch["status"] = "interrupted_on_resume"
        return [dispatch]

    def _restore_c1_scheduler_provenance(self, previous: dict[str, Any]) -> None:
        scheduler = previous.get("c1_scheduler")
        execution = previous.get("execution_order")
        latin = previous.get("latin_square")
        if not isinstance(scheduler, dict):
            raise ValueError("Cannot resume C1 without outer scheduler provenance")
        if not isinstance(execution, list) or not all(
            isinstance(row, dict) for row in execution
        ):
            raise ValueError("Cannot resume C1 without canonical execution_order")
        if not isinstance(latin, dict) or not isinstance(latin.get("blocks"), list):
            raise ValueError("Cannot resume C1 without Latin-square provenance")
        if scheduler.get("requested_outer_workers") != self.c1_outer_worker:
            raise ValueError("Cannot resume C1 with changed outer worker provenance")
        schema = scheduler.get("schema_version")
        if schema not in {1, C1_OUTER_SCHEDULER_SCHEMA}:
            raise ValueError("Cannot resume C1 with unsupported outer scheduler schema")

        restored_rows = [copy.deepcopy(row) for row in execution]
        start_ordinals: list[int] = []
        completion_ordinals: list[int] = []
        for row in restored_rows:
            if any(row.get(field) is None for field in _C1_EXECUTION_IDENTITY_FIELDS):
                raise ValueError("Cannot resume C1 with incomplete registration provenance")
            if schema == 1:
                row["dispatch_history"] = self._legacy_dispatch_history(row)
            elif not isinstance(row.get("dispatch_history"), list) or not all(
                isinstance(item, dict) for item in row["dispatch_history"]
            ):
                raise ValueError("Cannot resume C1 with invalid dispatch_history")
            if row["dispatch_history"]:
                latest = row["dispatch_history"][-1]
                if (
                    latest.get("status") == "running"
                    and latest.get("completion_ordinal") is None
                ):
                    latest["status"] = "interrupted_on_resume"
                    row["status"] = "interrupted_on_resume"
            for dispatch_index, dispatch in enumerate(row["dispatch_history"]):
                if dispatch.get("dispatch_ordinal") != dispatch_index:
                    raise ValueError("Cannot resume C1 with invalid dispatch ordinals")
                start = dispatch.get("start_ordinal")
                started_ns = dispatch.get("started_monotonic_ns")
                if (
                    not isinstance(start, int)
                    or isinstance(start, bool)
                    or start < 0
                    or not isinstance(started_ns, int)
                    or isinstance(started_ns, bool)
                    or started_ns < 0
                ):
                    raise ValueError("Cannot resume C1 with invalid dispatch start provenance")
                start_ordinals.append(start)
                completion = dispatch.get("completion_ordinal")
                completed_ns = dispatch.get("completed_monotonic_ns")
                if completion is None and completed_ns is None:
                    if dispatch.get("status") not in {
                        "running",
                        "interrupted_on_resume",
                    }:
                        raise ValueError(
                            "Cannot resume C1 with missing dispatch completion provenance"
                        )
                elif (
                    not isinstance(completion, int)
                    or isinstance(completion, bool)
                    or completion < 0
                    or not isinstance(completed_ns, int)
                    or isinstance(completed_ns, bool)
                    or completed_ns < 0
                ):
                    raise ValueError(
                        "Cannot resume C1 with invalid dispatch completion provenance"
                    )
                else:
                    completion_ordinals.append(completion)

        if len(start_ordinals) != len(set(start_ordinals)) or sorted(
            start_ordinals
        ) != list(range(len(start_ordinals))):
            raise ValueError("Cannot resume C1 with non-contiguous outer start ordinals")
        if len(completion_ordinals) != len(set(completion_ordinals)) or sorted(
            completion_ordinals
        ) != list(range(len(completion_ordinals))):
            raise ValueError(
                "Cannot resume C1 with non-contiguous outer completion ordinals"
            )
        for field in ("effective_outer_workers", "max_active_invocations"):
            value = scheduler.get(field)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or not 0 <= value <= self.c1_outer_worker
            ):
                raise ValueError(f"Cannot resume C1 with invalid {field}")

        restored_scheduler = copy.deepcopy(scheduler)
        restored_scheduler["schema_version"] = C1_OUTER_SCHEDULER_SCHEMA
        self.c1_outer_scheduler = restored_scheduler
        self.execution_order = restored_rows
        self.latin_square = copy.deepcopy(latin)

        starts = [
            dispatch.get("start_ordinal")
            for row in restored_rows
            for dispatch in row.get("dispatch_history", [])
            if isinstance(dispatch.get("start_ordinal"), int)
            and not isinstance(dispatch.get("start_ordinal"), bool)
        ]
        completions = [
            dispatch.get("completion_ordinal")
            for row in restored_rows
            for dispatch in row.get("dispatch_history", [])
            if isinstance(dispatch.get("completion_ordinal"), int)
            and not isinstance(dispatch.get("completion_ordinal"), bool)
        ]
        self._outer_started = max(starts, default=-1) + 1
        self._outer_completed = max(completions, default=-1) + 1
        self._outer_active = 0

    def _completed(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        task_id: str | None = None,
    ) -> bool:
        if not self.resume:
            # Imported trial rows are intentionally usable in a fresh clean root.
            if task_id is None or self.c1_source_index is None:
                return False
        for row in self.index.get(arm, []):
            if (
                row.get("model") != model
                or row.get("seed") != seed
                or (
                    row.get("status") != "ok"
                    and not (
                        arm in C1_EXECUTION_ARMS and row.get("status") == "error"
                    )
                )
                or (task_id is not None and row.get("task_id") != task_id)
                or (task_id is None and row.get("task_id") is not None)
            ):
                continue
            report = row.get("report")
            if report:
                report_path = Path(str(report))
                if not report_path.is_absolute():
                    report_path = self.base / report_path
                if report_path.exists():
                    if arm == "c2_memory_baseline":
                        if _c2_memory_report_issue(
                            report_path,
                            expected_sha256=row.get("report_sha256"),
                        ) is None:
                            return True
                        continue
                    if arm not in C1_EXECUTION_ARMS:
                        return True
                    try:
                        report_raw = report_path.read_bytes()
                        payload = json.loads(report_raw)
                    except Exception:
                        continue
                    expected_hash = row.get("report_sha256")
                    if not (
                        isinstance(expected_hash, str)
                        and len(expected_hash) == 64
                        and hashlib.sha256(report_raw).hexdigest() == expected_hash
                    ):
                        continue
                    if (
                        row.get("status") == "ok"
                        and payload.get("complete") is True
                    ):
                        return True
                    source_provenance = row.get("source_provenance")
                    if (
                        row.get("status") == "error"
                        and row.get("imported") is True
                        and isinstance(source_provenance, dict)
                        and source_provenance.get("source_status") == "error"
                        and source_provenance.get("completion_class")
                        == "exact_usage_exhausted_operational_failure"
                    ):
                        results = payload.get("results")
                        result = results[0] if isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict) else None
                        if (
                            isinstance(result, dict)
                            and result.get("task_id") == row.get("task_id")
                            and self._registered_source_error_kind(
                                result=result, report=payload,
                                source={"c1_task_timeout_s": self.c1_task_timeout_s},
                                task_id=str(row.get("task_id") or ""),
                            ) in {"exact_usage_task_timeout", "exact_usage_retryable_infrastructure"}
                        ):
                            return True
                    if row.get("status") == "error":
                        current_attempt = row.get("trial_attempt")
                        has_current_overhead = any(
                            overhead.get("arm") == arm
                            and overhead.get("model") == row.get("model")
                            and overhead.get("seed") == row.get("seed")
                            and overhead.get("task_id") == row.get("task_id")
                            and overhead.get("trial_attempt") == current_attempt
                            and overhead.get("source_provenance") is None
                            for overhead in self.c1_operational_overhead
                            if isinstance(overhead, dict)
                        )
                        _, terminal_issues = (
                            p5_aggregate._local_settled_terminal_evidence(
                                arm=arm,
                                entry=row,
                                report=payload,
                                actual_report_sha=hashlib.sha256(
                                    report_raw
                                ).hexdigest(),
                                has_current_overhead=has_current_overhead,
                            )
                        )
                        if not terminal_issues:
                            return True
                        continue
                    source_provenance = row.get("source_provenance")
                    completion_class = (
                        source_provenance.get("completion_class")
                        if isinstance(source_provenance, dict)
                        else None
                    )
                    results = payload.get("results")
                    if not (
                        row.get("imported") is True
                        and isinstance(source_provenance, dict)
                        and source_provenance.get("source_status") == "error"
                        and completion_class
                        in {
                            "exact_usage_nonretryable_task_timeout",
                            "exact_usage_nonretryable_model_failure",
                            "exact_usage_exhausted_retryable_infrastructure",
                        }
                        and isinstance(
                            source_provenance.get("source_sidecar_sha256"), str
                        )
                        and len(source_provenance["source_sidecar_sha256"]) == 64
                        and isinstance(results, list)
                        and len(results) == 1
                        and isinstance(results[0], dict)
                        and results[0].get("task_id") == row.get("task_id")
                    ):
                        continue
                    if (
                        self._registered_source_model_failure_kind(
                            result=results[0],
                            report=payload,
                        )
                        == completion_class
                    ):
                        return True
        return False

    def _quarantined_trial(
        self, arm: str, *, model: str, seed: int, task_id: str
    ) -> bool:
        return any(
            row.get("model") == model
            and row.get("seed") == seed
            and row.get("task_id") == task_id
            and row.get("status") == "quarantined_unknown_usage"
            for row in self.index.get(arm, [])
        )

    def _index_payload(self) -> dict[str, Any]:
        return {
            "experiment_id": "p5_scale",
            "run_id": self.run_id,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "git_commit": self.git_commit,
            "manifest_hash": self.manifest_hash,
            "task_manifest_hash": self.task_manifest_hash,
            "harness_hash": self.harness_hash,
            "c1_application_revisions": self.c1_application_revisions,
            "c1_environment": self.c1_environment,
            "c2_harness_hash": self.c2_harness_hash,
            "c2_manifest_fingerprint": self.c2_manifest_fingerprint,
            "task_ids": self.task_ids,
            "manifest_name": self.manifest.get("name"),
            "agent_kind": self.agent_kind,
            "headless": self.headless,
            "models": self.models,
            "seeds": self.seeds,
            "memory_workers": self.memory_workers,
            "c1_outer_worker": self.c1_outer_worker,
            "c1_inner_worker": self.c1_inner_worker,
            "c1_task_timeout_s": self.c1_task_timeout_s,
            "temperature": self.temperature if self.agent_kind == "llm" else None,
            "api_provider": self._api_provider_provenance(),
            "provider_timeout_s": (
                self.provider_timeout_s
                if any(arm in C1_EXECUTION_ARMS for arm in self.arms)
                else None
            ),
            "visual_bridge": self.manifest.get("visual_bridge"),
            "visual_judge_model": (
                self.manifest.get("visual_judge", {}).get("model")
                if isinstance(self.manifest.get("visual_judge"), dict)
                else self.manifest.get("visual_judge_model")
            ),
            "model_request_profiles": self.model_request_profiles,
            "model_request_profiles_sha256": self.model_request_profiles_sha256,
            "runtime_lock": self.runtime_lock_policy,
            "usage_accounting": self.usage_accounting,
            "c1_trial_persistence": self.c1_trial_persistence,
            "c1_start_gate_protocol": self.c1_start_gate_protocol,
            "c1_background_pixel_protocol": self.c1_background_pixel_protocol,
            "c1_background_pixel_protocol_sha256": self.c1_background_pixel_protocol_sha256,
            "c1_prompt_revisions": (
                self._c1_prompt_revision_provenance()
                if any(arm in C1_EXECUTION_ARMS for arm in self.arms)
                else None
            ),
            "c1_random_controls": (
                self._c1_random_control_provenance()
                if any(arm in C1_EXECUTION_ARMS for arm in self.arms)
                else None
            ),
            "c1_source_index_request": (
                str(self.c1_source_index) if self.c1_source_index else None
            ),
            "c1_source_contract_migration_request": (
                str(self.c1_source_contract_migration)
                if self.c1_source_contract_migration
                else None
            ),
            "c1_source_contract_migration": (
                self.c1_source_contract_migration_metadata
            ),
            "c1_sources": self.c1_sources,
            "c1_operational_overhead": self.c1_operational_overhead,
            "c1_cache_policy": self._c1_cache_policy(),
            "cost_verified": bool(self.manifest.get("cost", {}).get("verified", False)),
            "preflight": self.preflight_metadata,
            "calibration": self.calibration_metadata,
            "c1_sandbox_preflight": self.c1_sandbox_preflight,
            "c1_unity_canary": self.c1_unity_canary,
            "c1_unity_serialized": self.c1_unity_serialized,
            "c1_unity_serialized_benchmarks": list(
                self.c1_unity_serialized_benchmarks
            ),
            "c1_scheduler": self.c1_outer_scheduler,
            "c1_resource_scheduler": self.c1_ready_scheduler.snapshot(),
            "execution_order": self.execution_order,
            "latin_square": self.latin_square,
            "arms_run": self.arms,
            "arms": self.index,
        }

    def _write_index_unlocked(self) -> dict[str, Any]:
        payload = self._index_payload()
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._index_path.with_name(self._index_path.name + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(temporary, self._index_path)
        return payload

    def _run_meta(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        task_id: str | None = None,
        attempt: int = 0,
    ) -> dict[str, Any]:
        c2_profile_provenance = (
            {
                "model_request_profiles": self.model_request_profiles,
                "model_request_profiles_sha256": self.model_request_profiles_sha256,
                "model_request_profile": self._active_model_request_provenance(model),
            }
            if arm == "c2_memory_baseline"
            else {}
        )
        return run_context.run_metadata(
            experiment_id=f"p5:{arm}",
            run_id=(
                f"{self.run_id}:{arm}:{model or 'na'}:"
                f"{seed if seed is not None else 'na'}:"
                f"{task_id or 'all'}:attempt{attempt}"
            ),
            task_manifest_hash=self.task_manifest_hash,
            git_root=self.base,
            extra={
                "arm": arm,
                "model": model,
                "seed": seed,
                "task_id": task_id,
                "trial_attempt": attempt,
                "usage_accounting": self.usage_accounting,
                "c1_trial_persistence": self.c1_trial_persistence,
                "c1_start_gate_protocol": self.c1_start_gate_protocol,
                "c1_background_pixel_protocol": self.c1_background_pixel_protocol,
                "c1_background_pixel_protocol_sha256": self.c1_background_pixel_protocol_sha256,
                "c1_application_revisions": (
                    self.c1_application_revisions if arm in C1_EXECUTION_ARMS else None
                ),
                "c1_environment": self.c1_environment if arm in C1_EXECUTION_ARMS else None,
                "c1_prompt_revisions": (
                    self._c1_prompt_revision_provenance()
                    if arm in C1_EXECUTION_ARMS
                    else None
                ),
                "c1_random_controls": (
                    self._c1_random_control_provenance()
                    if arm in C1_EXECUTION_ARMS
                    else None
                ),
                "p5_run_id": self.run_id,
                "c2_harness_hash": (
                    self._freeze_c2_harness_hash()
                    if arm == "c2_memory_baseline"
                    else None
                ),
                "c2_manifest_fingerprint": (
                    self.c2_manifest_fingerprint
                    if arm == "c2_memory_baseline"
                    else None
                ),
                "preflight": self._preflight_provenance(),
                "calibration": self.calibration_metadata,
                "latin_square": getattr(self._latin_local, "value", None),
                "runtime_lock": self.runtime_lock_policy,
                "provider_timeout_s": (
                    self.provider_timeout_s if arm in C1_EXECUTION_ARMS else None
                ),
                **c2_profile_provenance,
            },
        )

    def _freeze_c1_reproducibility(self) -> dict[str, Any] | None:
        """Freeze explicit host revisions and the observed machine contract."""

        if not isinstance(self.manifest.get("c1_reproducibility"), dict):
            return None
        current_environment = validate_c1_environment(
            self.base,
            manifest=self.manifest,
        )
        current_revisions = c1_application_revisions(self.base)
        if (
            self.c1_environment is not None
            and self.c1_environment != current_environment
        ):
            raise ValueError(
                "C1 environment changed during the run; use a new report directory"
            )
        if (
            self.c1_application_revisions is not None
            and self.c1_application_revisions != current_revisions
        ):
            raise ValueError(
                "C1 application revisions changed during the run; use a new report directory"
            )
        self.c1_environment = current_environment
        self.c1_application_revisions = current_revisions
        return current_environment

    def _freeze_c1_harness_hash(self) -> str:
        """Freeze the code hash used by every invocation in this run."""

        self._freeze_c1_reproducibility()
        current = c1_harness_hash(self.base)
        if self.harness_hash is not None and self.harness_hash != current:
            raise ValueError(
                "C1 harness changed during the run; use a new report directory"
            )
        self.harness_hash = current
        return current

    def _freeze_c2_harness_hash(self) -> str:
        """Bind C2 checkpoints to the exact evaluator, task set, and game pins."""

        current = c2_harness_hash(self.base)
        if self.c2_harness_hash is not None and self.c2_harness_hash != current:
            raise ValueError(
                "C2 harness changed during the run; use a new report directory"
            )
        self.c2_harness_hash = current
        return current

    def _calibration_provenance(self) -> dict[str, Any] | None:
        if not isinstance(self.calibration_metadata, dict):
            return None
        # The artifact path is deliberately excluded: an identical artifact copied to
        # another path is still the same calibration. Its byte hash and all scientific
        # gate fields remain bound to the invocation.
        fields = (
            "schema_version",
            "schema",
            "passed",
            "generated_at_utc",
            "artifact_sha256",
            "git_commit",
            "manifest_hash",
            "harness_hash",
            "c1_application_revisions",
            "c1_environment",
            "api_provider",
            "provider_timeout_s",
            "observer_model",
            "grounder_model",
            "background_pixel_protocol_sha256",
            "backends",
        )
        provenance = {
            field: self.calibration_metadata.get(field) for field in fields
        }
        if "provider_timeout_s" not in self.calibration_metadata:
            provenance.pop("provider_timeout_s", None)
        return provenance

    def _preflight_provenance(self) -> dict[str, Any] | None:
        if not isinstance(self.preflight_metadata, dict):
            return None
        fields = (
            "complete",
            "generated_at_utc",
            "artifact_sha256",
            "git_commit",
            "manifest_hash",
            "harness_hash",
            "c1_application_revisions",
            "c1_environment",
            "api_provider",
            "provider_timeout_s",
            "model_request_profiles",
            "model_request_profiles_sha256",
            "probe_count",
            "initial_max_tokens",
            "retry_max_tokens",
            "retry_rule",
            "temperature_contract",
            "results",
        )
        provenance = {field: self.preflight_metadata.get(field) for field in fields}
        if "provider_timeout_s" not in self.preflight_metadata:
            provenance.pop("provider_timeout_s", None)
        return provenance

    @staticmethod
    def _require_fresh_artifact(
        payload: dict[str, Any], *, max_age_hours: Any, label: str
    ) -> None:
        if max_age_hours is None:
            return
        try:
            maximum = float(max_age_hours)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"{label} max_age_hours must be positive") from error
        if maximum <= 0:
            raise RuntimeError(f"{label} max_age_hours must be positive")
        raw = payload.get("generated_at_utc")
        if not isinstance(raw, str):
            raise RuntimeError(f"{label} artifact is missing generated_at_utc")
        try:
            generated = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if generated.tzinfo is None:
                raise ValueError("timezone missing")
            age_s = (datetime.now(timezone.utc) - generated.astimezone(timezone.utc)).total_seconds()
        except ValueError as error:
            raise RuntimeError(f"{label} artifact generated_at_utc is invalid") from error
        if age_s < -300 or age_s > maximum * 3600:
            raise RuntimeError(f"{label} artifact is outside its freshness window")

    def _api_provider_provenance(self) -> dict[str, Any]:
        end_user = (
            os.environ.get("RELAY_END_USER", "")
            if self.api_key_env == "RELAY_API_KEY"
            else ""
        )
        api_cfg = self.manifest.get("api")
        return {
            "name": api_cfg.get("provider") if isinstance(api_cfg, dict) else None,
            "base_url": self.base_url.rstrip("/"),
            "end_user_sha256": (
                hashlib.sha256(end_user.encode("utf-8")).hexdigest()
                if end_user
                else None
            ),
        }

    def _model_request_provenance(self, model: str | None) -> dict[str, Any] | None:
        if not model:
            return None
        canonical = canonical_model_name(model)
        profile = self.model_request_profiles.get(canonical, PROVIDER_DEFAULT)
        return ModelRequestProfile(canonical, profile).provenance()

    def _active_model_request_provenance(
        self, model: str | None
    ) -> dict[str, Any] | None:
        """Return the effective profile after proving the manifest is active."""

        active_profiles = configured_model_request_profiles()
        if active_profiles != self.model_request_profiles:
            raise RuntimeError(
                "P5 model request profiles are not active; invoke the orchestrator via run()"
            )
        return self._model_request_provenance(model)

    def _c1_task_set_provenance(self, tasks_path: str | Path) -> dict[str, Any]:
        path = Path(tasks_path).expanduser()
        if not path.is_absolute():
            path = (self.base / path).resolve()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list):
            raise ValueError(f"C1 task file must contain a tasks list: {path}")
        task_ids = [str(task.get("id")) for task in tasks if isinstance(task, dict) and task.get("id")]
        if len(task_ids) != len(tasks) or len(task_ids) != len(set(task_ids)):
            raise ValueError(f"C1 task file has missing or duplicate task ids: {path}")
        if self.task_ids is not None:
            valid_selected_set = task_ids == self.task_ids
            valid_atomic_trial = bool(
                self.c1_trial_persistence is not None
                and len(task_ids) == 1
                and task_ids[0] in self.task_ids
            )
            if not valid_selected_set and not valid_atomic_trial:
                raise ValueError(
                    "C1 selected task file does not match the requested task set "
                    "or one registered atomic trial"
                )

        raw_source = payload.get("source_task_file") if isinstance(payload, dict) else None
        source = Path(str(raw_source)).expanduser() if raw_source else path
        if not source.is_absolute():
            source = (self.base / source).resolve()
        return {
            "task_file": str(path),
            "task_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source_task_file": str(source),
            "source_task_file_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "task_ids": task_ids,
            "task_set_hash": run_context.manifest_hash({"tasks": tasks}),
        }

    def _c1_invocation_context(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        tasks_path: str | Path,
        invocation: dict[str, Any] | None,
        task_id: str | None = None,
        attempt: int = 0,
    ) -> dict[str, Any]:
        self._verify_preflight_artifact_unchanged()
        self._verify_calibration_artifact_unchanged()
        self._freeze_c1_reproducibility()
        judge_values = visual_judge_env_values(self.manifest)
        return {
            "schema_version": 6 if self.c1_trial_persistence is not None else 4,
            "kind": "p5_c1_invocation_context",
            "run_id": self.run_id,
            "arm": arm,
            "model": model,
            "seed": seed,
            "task_id": task_id,
            "trial_attempt": attempt,
            "usage_accounting": self.usage_accounting,
            "c1_trial_persistence": self.c1_trial_persistence,
            "c1_start_gate_protocol": self.c1_start_gate_protocol,
            "c1_background_pixel_protocol": self.c1_background_pixel_protocol,
            "c1_background_pixel_protocol_sha256": self.c1_background_pixel_protocol_sha256,
            "c1_prompt_revisions": self._c1_prompt_revision_provenance(),
            "c1_random_controls": self._c1_random_control_provenance(),
            "git_commit": self.git_commit,
            "manifest_hash": self.manifest_hash,
            "task_manifest_hash": self.task_manifest_hash,
            "harness_hash": self._freeze_c1_harness_hash(),
            "c1_application_revisions": self.c1_application_revisions,
            "c1_environment": self.c1_environment,
            "preflight": self._preflight_provenance(),
            "calibration": self._calibration_provenance(),
            "task_set": self._c1_task_set_provenance(tasks_path),
            "report_file": _ARM_REPORT_FILE[arm],
            "agent_kind": self.agent_kind,
            "headless": self.headless,
            "temperature": self.temperature if self.agent_kind == "llm" else None,
            "api_provider": self._api_provider_provenance(),
            "provider_timeout_s": self.provider_timeout_s,
            "task_timeout_s": self.c1_task_timeout_s,
            "model_request_profiles": self.model_request_profiles,
            "model_request_profiles_sha256": self.model_request_profiles_sha256,
            "model_request_profile": self._model_request_provenance(model),
            "runtime_lock": self.runtime_lock_policy,
            "visual_judge": {
                "model": judge_values.get("WARPTEST_VISUAL_JUDGE_MODEL"),
                "base_url": judge_values.get("WARPTEST_VISUAL_JUDGE_BASE_URL"),
            },
            "cache_policy": self._c1_cache_policy(),
            "invocation": invocation or {},
        }

    def _c1_cache_policy(self) -> dict[str, Any]:
        policy = {
            "root": str((self.run_dir / "c1-artifact-cache").resolve()),
            "golden_and_reference": "run_scoped",
            "visual_judge": "disabled",
            "self_synthesis": str(
                (self.run_dir / "c1-self-synthesis-cache").resolve()
            ),
        }
        if "c1_state_minimal_agent" in self.arms:
            diagnostic_cfg = self._arm_cfg("c1_state_minimal_agent")
            transition_source = str(
                diagnostic_cfg.get("transition_action_source")
                or "minimal_agent_v1"
            )
            if transition_source == "source_agent_v2":
                policy["source_transition_actions"] = str(
                    (self.run_dir / "c1-source-action-cache").resolve()
                )
            else:
                policy["minimal_smoke_actions"] = str(
                    (self.run_dir / "c1-minimal-action-cache").resolve()
                )
        if any(arm in {"c1_state", "c1_state_minimal_agent"} for arm in self.arms):
            policy["pair_start"] = str(
                (self.run_dir / "c1-pair-start-cache").resolve()
            )
        return policy

    def _c1_prompt_revision_provenance(self) -> dict[str, Any]:
        """Bind public task and Phase-B prompt revisions into run provenance."""

        selected_arms = [arm for arm in self.arms if arm in C1_EXECUTION_ARMS]
        arm_revisions = {
            arm: {
                key: str(self._arm_cfg(arm).get(key) or "")
                for key in (
                    "prompt_revision",
                    "phase_b_prompt_revision",
                    "source_action_prompt_revision",
                    "pair_start_protocol",
                )
            }
            for arm in selected_arms
        }
        task_revisions: dict[str, str] = {}
        if selected_arms:
            try:
                tasks = self._c1_task_records(self._arm_cfg(selected_arms[0]))
            except FileNotFoundError:
                # Private scheduling-unit seams may replace all producers before
                # creating a task file. A real C1 run validates and loads the task
                # set before dispatch, so it still fails closed there.
                tasks = []
            for task in tasks:
                task_revisions[str(task["id"])] = str(
                    task.get("prompt_revision") or ""
                )
        return {
            "arms": arm_revisions,
            "tasks": task_revisions,
        }

    def _c1_random_control_provenance(self) -> dict[str, dict[str, Any]]:
        selected_arms = [arm for arm in self.arms if arm in C1_EXECUTION_ARMS]
        if not selected_arms:
            return {}
        try:
            tasks = self._c1_task_records(self._arm_cfg(selected_arms[0]))
        except FileNotFoundError:
            return {}
        controls: dict[str, dict[str, Any]] = {}
        missing: list[str] = []
        for task in tasks:
            value = task.get("random_control")
            if not isinstance(value, dict) or not value:
                missing.append(str(task.get("id") or ""))
                continue
            controls[str(task["id"])] = copy.deepcopy(value)
        if missing and controls:
            raise ValueError(
                "C1 task random_control is only partially populated: "
                + ", ".join(missing)
            )
        if missing:
            return {}
        return controls

    @staticmethod
    def _validate_c1_invocation_context(path: Path, expected: dict[str, Any]) -> None:
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise ValueError(f"Cannot resume C1 invocation with unreadable provenance: {path}") from error
        if actual != expected:
            keys = sorted(
                key for key in set(actual if isinstance(actual, dict) else {}) | set(expected)
                if not isinstance(actual, dict) or actual.get(key) != expected.get(key)
            )
            detail = ", ".join(keys) if keys else "payload"
            raise ValueError(
                f"Cannot resume C1 invocation with changed provenance ({detail}); "
                "use a new report directory"
            )

    def _prepare_c1_invocation(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        invocation_dir: Path,
        tasks_path: str | Path,
        invocation: dict[str, Any] | None,
        task_id: str | None = None,
        attempt: int = 0,
    ) -> tuple[Path, dict[str, Any]]:
        """Atomically bind partial task results before a producer may inspect them."""

        expected = self._c1_invocation_context(
            arm,
            model=model,
            seed=seed,
            tasks_path=tasks_path,
            invocation=invocation,
            task_id=task_id,
            attempt=attempt,
        )
        context_path = invocation_dir / _C1_INVOCATION_CONTEXT_FILE
        report_path = invocation_dir / _ARM_REPORT_FILE[arm]
        if context_path.exists():
            if not self.resume:
                raise ValueError(
                    f"C1 invocation directory already exists: {invocation_dir}; "
                    "pass --resume or use a new report directory"
                )
            self._validate_c1_invocation_context(context_path, expected)
        else:
            if report_path.exists():
                raise ValueError(
                    "Cannot resume legacy C1 partial report without invocation provenance; "
                    "use a new report directory"
                )
            # Persist the run-level gate first, then the per-invocation sidecar. A crash
            # between these writes cannot expose a partial report because the producer
            # has not been called yet.
            with self._index_lock:
                self._write_index_unlocked()
            invocation_dir.mkdir(parents=True, exist_ok=True)
            temporary = context_path.with_name(context_path.name + ".tmp")
            temporary.write_text(
                json.dumps(expected, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, context_path)
        # Refresh the index with the current Latin-square position before any paid call.
        with self._index_lock:
            self._write_index_unlocked()
        return context_path, expected

    def _c2_invocation_context(
        self,
        *,
        model: str | None,
        seed: int | None,
        invocation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Provenance sidecar for one resumable C2 model/seed suite."""

        return {
            "schema_version": 3,
            "kind": "p5_c2_memory_invocation_context",
            "run_id": self.run_id,
            "arm": "c2_memory_baseline",
            "model": model,
            "seed": seed,
            "git_commit": self.git_commit,
            # Scoped to the C2 arm's own manifest fields (not the full
            # manifest_hash/task_manifest_hash) so an unrelated, concurrent
            # C1/C3 manifest edit cannot block resuming this invocation.
            "c2_manifest_fingerprint": self.c2_manifest_fingerprint,
            "c2_harness_hash": self._freeze_c2_harness_hash(),
            "report_file": _ARM_REPORT_FILE["c2_memory_baseline"],
            "agent_kind": self.agent_kind,
            "headless": self.headless,
            "temperature": self.temperature if self.agent_kind == "llm" else None,
            "api_provider": self._api_provider_provenance(),
            "model_request_profiles": self.model_request_profiles,
            "model_request_profiles_sha256": self.model_request_profiles_sha256,
            "model_request_profile": self._active_model_request_provenance(model),
            "runtime_lock": self.runtime_lock_policy,
            "invocation": invocation or {},
        }

    @staticmethod
    def _validate_c2_invocation_context(path: Path, expected: dict[str, Any]) -> None:
        try:
            actual = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise ValueError(f"Cannot resume C2 invocation with unreadable provenance: {path}") from error
        if actual != expected:
            keys = sorted(
                key for key in set(actual if isinstance(actual, dict) else {}) | set(expected)
                if not isinstance(actual, dict) or actual.get(key) != expected.get(key)
            )
            detail = ", ".join(keys) if keys else "payload"
            raise ValueError(
                f"Cannot resume C2 invocation with changed provenance ({detail}); "
                "use a new report directory"
            )

    def _prepare_c2_invocation(
        self,
        *,
        model: str | None,
        seed: int | None,
        invocation_dir: Path,
        invocation: dict[str, Any] | None,
    ) -> tuple[Path, dict[str, Any]]:
        """Persist C2 identity before its first task checkpoint is written."""

        expected = self._c2_invocation_context(
            model=model,
            seed=seed,
            invocation=invocation,
        )
        context_path = invocation_dir / _C2_INVOCATION_CONTEXT_FILE
        report_path = invocation_dir / _ARM_REPORT_FILE["c2_memory_baseline"]
        if context_path.exists():
            if not self.resume:
                raise ValueError(
                    f"C2 invocation directory already exists: {invocation_dir}; "
                    "pass --resume or use a new report directory"
                )
            self._validate_c2_invocation_context(context_path, expected)
        else:
            if report_path.exists():
                raise ValueError(
                    "Cannot resume legacy C2 report without invocation provenance; "
                    "use a new report directory"
                )
            if invocation_dir.exists():
                legacy_markers = (
                    "evo-progress.json",
                    "evo-synthesis.json",
                    "evo-memory.json",
                    "frozen-train-memory.json",
                )
                if any(
                    next(invocation_dir.rglob(marker), None) is not None
                    for marker in legacy_markers
                ):
                    raise ValueError(
                        "Cannot resume legacy C2 partial artifacts without invocation provenance; "
                        "use a new report directory"
                    )
            # Write the initial index and then the sidecar before any paid call.  A
            # crash now is safely resumable because no phase output exists yet.
            with self._index_lock:
                self._write_index_unlocked()
            invocation_dir.mkdir(parents=True, exist_ok=True)
            temporary = context_path.with_name(context_path.name + ".tmp")
            temporary.write_text(
                json.dumps(expected, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, context_path)
        with self._index_lock:
            self._write_index_unlocked()
        return context_path, expected

    def _expected_preflight_probes(self) -> list[dict[str, str]]:
        # ``--models`` changes the routes that will actually incur paid calls,
        # so its preflight must follow the effective model set rather than the
        # manifest's default quartet. Visual roles stay manifest-registered.
        models = self.models
        expected = [
            {
                "name": f"decision:{str(model).split('@', 1)[0]}",
                "model": str(model).split("@", 1)[0],
                "modality": "vision",
            }
            for model in models
        ]
        diagnostic_cfg = (
            self.manifest.get("arms", {}).get("c1_state_minimal_agent", {})
            if isinstance(self.manifest.get("arms"), dict)
            else {}
        )
        if (
            "c1_state_minimal_agent" in self.arms
            and isinstance(diagnostic_cfg, dict)
            and diagnostic_cfg.get("transition_action_source") == "source_agent_v2"
        ):
            expected.extend(
                {
                    "name": f"source_transition:{str(model).split('@', 1)[0]}",
                    "model": str(model).split("@", 1)[0],
                    "modality": "text",
                }
                for model in models
            )
        expected.extend(
            {
                "name": f"synthesis:{str(model).split('@', 1)[0]}",
                "model": str(model).split("@", 1)[0],
                "modality": "text",
            }
            for model in models
        )
        bridge = (
            self.manifest.get("visual_bridge")
            if isinstance(self.manifest.get("visual_bridge"), dict)
            else {}
        )
        judge = (
            self.manifest.get("visual_judge")
            if isinstance(self.manifest.get("visual_judge"), dict)
            else {}
        )
        observer = str(bridge.get("observer_model") or "claude-sonnet-5@relay")
        judge_model = str(
            judge.get("model")
            or self.manifest.get("visual_judge_model")
            or "qwen3-vl-flash@relay"
        )
        expected.append(
            {
                "name": "visual_observer",
                "model": observer.split("@", 1)[0],
                "modality": "vision",
            }
        )
        expected.extend(
            {
                "name": probe.name,
                "model": probe.model.split("@", 1)[0],
                "modality": "vision",
            }
            for probe in grounder_preflight_probes(
                models, observer=observer, configured=bridge.get("grounder_model")
            )
        )
        expected.append(
            {
                "name": "visual_judge",
                "model": judge_model.split("@", 1)[0],
                "modality": "vision",
            }
        )
        return expected

    @staticmethod
    def _validate_preflight_usage(value: Any) -> bool:
        if not isinstance(value, dict):
            return False
        integer_names = (
            "prompt_tokens",
            "completion_tokens",
            "cached_prompt_tokens",
            "total_tokens",
            "reasoning_tokens_observed",
            "reasoning_tokens_lower_bound",
            "reasoning_tokens_upper_bound",
            "reasoning_detail_missing_calls",
            "reasoning_detail_unknown_completion_tokens",
            "reasoning_detail_expected_completion_tokens",
        )
        if any(
            isinstance(value.get(name), bool)
            or not isinstance(value.get(name), int)
            or value[name] < 0
            for name in integer_names
        ):
            return False
        reasoning = value.get("reasoning_tokens")
        if reasoning is not None and (
            isinstance(reasoning, bool)
            or not isinstance(reasoning, int)
            or reasoning < 0
            or reasoning > value["completion_tokens"]
        ):
            return False
        fraction = value.get("reasoning_detail_unknown_fraction")
        if isinstance(fraction, bool) or not isinstance(fraction, (int, float)):
            return False
        expected = value["reasoning_detail_expected_completion_tokens"]
        unknown = value["reasoning_detail_unknown_completion_tokens"]
        expected_fraction = float(unknown) / float(expected) if expected else 0.0
        return bool(
            value["total_tokens"]
            == value["prompt_tokens"] + value["completion_tokens"]
            and value["cached_prompt_tokens"] <= value["prompt_tokens"]
            and value.get("reasoning_detail_tolerance_fraction")
            == cost.REASONING_DETAIL_TOLERANCE_FRACTION
            and abs(float(fraction) - expected_fraction) < 1e-12
            and value.get("reasoning_detail_within_tolerance")
            == (expected_fraction <= cost.REASONING_DETAIL_TOLERANCE_FRACTION)
            and value.get("reasoning_detail_complete")
            == (value["reasoning_detail_missing_calls"] == 0)
            and value["reasoning_tokens_lower_bound"]
            == value["reasoning_tokens_observed"]
            and value["reasoning_tokens_upper_bound"]
            == value["reasoning_tokens_observed"] + unknown
            and (
                reasoning == value["reasoning_tokens_observed"]
                if value["reasoning_detail_missing_calls"] == 0
                else reasoning is None
            )
        )

    @staticmethod
    def _sum_preflight_usage(items: list[dict[str, Any]]) -> dict[str, Any]:
        result = {
            name: sum(int(item.get(name, 0) or 0) for item in items)
            for name in (
                "prompt_tokens",
                "completion_tokens",
                "cached_prompt_tokens",
                "total_tokens",
            )
        }
        observed = sum(
            int(item.get("reasoning_tokens_observed", 0) or 0) for item in items
        )
        missing = sum(
            int(item.get("reasoning_detail_missing_calls", 0) or 0)
            for item in items
        )
        unknown = sum(
            int(item.get("reasoning_detail_unknown_completion_tokens", 0) or 0)
            for item in items
        )
        expected = sum(
            int(item.get("reasoning_detail_expected_completion_tokens", 0) or 0)
            for item in items
        )
        fraction = float(unknown) / float(expected) if expected else 0.0
        result.update(
            reasoning_tokens=None if missing else observed,
            reasoning_tokens_observed=observed,
            reasoning_tokens_lower_bound=observed,
            reasoning_tokens_upper_bound=observed + unknown,
            reasoning_detail_complete=missing == 0,
            reasoning_detail_missing_calls=missing,
            reasoning_detail_unknown_completion_tokens=unknown,
            reasoning_detail_expected_completion_tokens=expected,
            reasoning_detail_unknown_fraction=fraction,
            reasoning_detail_tolerance_fraction=(
                cost.REASONING_DETAIL_TOLERANCE_FRACTION
            ),
            reasoning_detail_within_tolerance=(
                fraction <= cost.REASONING_DETAIL_TOLERANCE_FRACTION
            ),
        )
        return result

    @classmethod
    def _validate_preflight_attempts(
        cls,
        result: dict[str, Any],
        *,
        initial_max_tokens: int,
        retry_max_tokens: int,
        expected_temperature: float | None,
        reasoning_disabled: bool = False,
    ) -> bool:
        attempts = result.get("attempt_records")
        count = result.get("attempts")
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count not in {1, 2, 3}
            or not isinstance(attempts, list)
            or len(attempts) != count
        ):
            return False
        usage_items: list[dict[str, Any]] = []
        successful: list[dict[str, Any]] = []
        fallback_indexes: list[int] = []
        for ordinal, attempt in enumerate(attempts, start=1):
            if (
                not isinstance(attempt, dict)
                or attempt.get("ordinal") != ordinal
                or attempt.get("max_tokens")
                not in {initial_max_tokens, retry_max_tokens}
                or attempt.get("temperature_configured") != expected_temperature
            ):
                return False
            if attempt.get("request_succeeded") is True:
                if (
                    not isinstance(attempt.get("content_nonempty"), bool)
                    or not isinstance(attempt.get("reasoning_observed"), bool)
                    or not isinstance(
                        attempt.get("reasoning_content_observed"), bool
                    )
                    or not isinstance(attempt.get("reasoning_token_anomaly"), bool)
                    or not isinstance(attempt.get("request_contract_passed"), bool)
                    or not isinstance(attempt.get("reasoning_exhausted"), bool)
                    or not cls._validate_preflight_usage(attempt.get("usage"))
                ):
                    return False
                expected_contract = bool(
                    attempt["content_nonempty"]
                    and not (
                        reasoning_disabled
                        and attempt["reasoning_content_observed"]
                    )
                )
                expected_anomaly = bool(
                    reasoning_disabled
                    and int(
                        attempt["usage"].get("reasoning_tokens_observed", 0) or 0
                    )
                    > 0
                )
                if (
                    attempt["request_contract_passed"] != expected_contract
                    or attempt["reasoning_token_anomaly"] != expected_anomaly
                ):
                    return False
                omitted = attempt.get("temperature_omitted_after_rejection") is True
                expected_requested = (
                    None if expected_temperature is None or omitted else expected_temperature
                )
                if attempt.get("temperature_requested") != expected_requested:
                    return False
                successful.append(attempt)
                usage_items.append(attempt["usage"])
                continue
            if (
                expected_temperature is None
                or attempt.get("request_succeeded") is not False
                or attempt.get("status_code") != 400
                or not isinstance(attempt.get("error_type"), str)
                or attempt.get("temperature_requested") != expected_temperature
                or attempt.get("temperature_fallback_triggered") is not True
            ):
                return False
            fallback_indexes.append(ordinal - 1)
            failed_usage = attempt.get("usage")
            if failed_usage is not None:
                if not cls._validate_preflight_usage(failed_usage):
                    return False
                usage_items.append(failed_usage)
        if len(fallback_indexes) > 1:
            return False
        if fallback_indexes:
            index = fallback_indexes[0]
            if (
                index + 1 >= len(attempts)
                or attempts[index + 1].get("request_succeeded") is not True
                or attempts[index + 1].get("max_tokens")
                != attempts[index].get("max_tokens")
                or attempts[index + 1].get("temperature_requested") is not None
                or attempts[index + 1].get(
                    "temperature_omitted_after_rejection"
                )
                is not True
                or result.get("temperature_fallback_triggered") is not True
                or result.get("temperature_omitted_after_rejection") is not True
            ):
                return False
        elif (
            result.get("temperature_fallback_triggered") is not False
            or result.get("temperature_omitted_after_rejection") is not False
            or any(attempt.get("request_succeeded") is not True for attempt in attempts)
        ):
            return False
        if not successful or len(successful) > 2:
            return False
        if successful[0].get("max_tokens") != initial_max_tokens:
            return False
        if len(successful) == 1:
            if successful[0]["content_nonempty"] is not True:
                return False
        else:
            first, second = successful
            if (
                second.get("max_tokens") != retry_max_tokens
                or first["content_nonempty"] is not False
                or first["reasoning_exhausted"] is not True
                or second["content_nonempty"] is not True
            ):
                return False
        reasoning_content_observed = any(
            bool(attempt.get("reasoning_content_observed"))
            for attempt in successful
        )
        reasoning_token_anomaly = bool(
            reasoning_disabled
            and int(
                result.get("usage", {}).get("reasoning_tokens_observed", 0) or 0
            )
            > 0
        )
        return bool(
            result.get("success") is True
            and result.get("request_contract_passed") is True
            and result.get("reasoning_content_observed")
            is reasoning_content_observed
            and (not reasoning_disabled or not reasoning_content_observed)
            and result.get("reasoning_token_anomaly")
            is reasoning_token_anomaly
            and result.get("usage") == cls._sum_preflight_usage(usage_items)
            and result.get("temperature_configured") == expected_temperature
            and result.get("finish_reason") == successful[-1].get("finish_reason")
        )

    def _require_preflight_gate(self) -> None:
        """Require an immutable, manifest-derived, usage-audited online preflight."""

        if self.agent_kind != "llm":
            return
        if self._preflight_gate_checked:
            self._verify_preflight_artifact_unchanged()
            return
        cfg = (
            self.manifest.get("preflight")
            if isinstance(self.manifest.get("preflight"), dict)
            else {}
        )
        if not cfg.get("required", False):
            self._preflight_gate_checked = True
            return
        if configured_model_request_profiles() != self.model_request_profiles:
            raise RuntimeError(
                "Formal C1 request profiles are not active; invoke the orchestrator via run()"
            )
        artifact = self.preflight_artifact
        if artifact is None:
            env_name = str(cfg.get("artifact_env") or "WARPTEST_C1_PREFLIGHT_ARTIFACT")
            raw = os.environ.get(env_name)
            artifact = Path(raw).expanduser() if raw else None
        if artifact is not None and not artifact.is_absolute():
            artifact = (self.base / artifact).resolve()
        if artifact is None or not artifact.is_file():
            raise RuntimeError(
                "Formal C1 run requires a complete online model preflight artifact"
            )
        try:
            artifact_raw = artifact.read_bytes()
            payload = json.loads(artifact_raw)
        except Exception as error:
            raise RuntimeError("Cannot read C1 preflight artifact") from error
        if payload.get("schema_version") != 4 or payload.get("complete") is not True:
            raise RuntimeError("C1 preflight artifact must be complete schema version 4")
        if not self._resume_index_loaded:
            self._require_fresh_artifact(
                payload,
                max_age_hours=cfg.get("max_age_hours"),
                label="C1 preflight",
            )
        initial_max_tokens = payload.get("initial_max_tokens")
        retry_max_tokens = payload.get("retry_max_tokens")
        if (
            initial_max_tokens != 8192
            or retry_max_tokens != 16384
            or payload.get("retry_rule")
            != "one_retry_only_if_empty_content_and_explicit_output_limit"
        ):
            raise RuntimeError("C1 preflight reasoning retry protocol mismatch")
        if payload.get("git_commit") != self.git_commit:
            raise RuntimeError("C1 preflight artifact git commit does not match this run")
        if payload.get("manifest_hash") != self.manifest_hash:
            raise RuntimeError("C1 preflight artifact manifest hash does not match this run")
        if payload.get("usage_accounting") != self.usage_accounting:
            raise RuntimeError("C1 preflight usage accounting policy does not match this run")
        if payload.get("api_provider") != self._api_provider_provenance():
            raise RuntimeError("C1 preflight provider routing does not match this run")
        if (
            self.provider_timeout_registered
            and payload.get("provider_timeout_s") != self.provider_timeout_s
        ):
            raise RuntimeError("C1 preflight provider timeout does not match this run")
        expected_temperature_contract = {
            "configured": self.temperature,
            "applies_to": "decision_probes_only",
            "fallback": "omit_only_after_explicit_http_400_temperature_rejection",
        }
        if payload.get("temperature_contract") != expected_temperature_contract:
            raise RuntimeError("C1 preflight temperature contract does not match this run")
        if (
            payload.get("model_request_profiles") != self.model_request_profiles
            or payload.get("model_request_profiles_sha256")
            != self.model_request_profiles_sha256
        ):
            raise RuntimeError("C1 preflight model request profiles do not match this run")
        current_harness = self._freeze_c1_harness_hash()
        if payload.get("harness_hash") != current_harness:
            raise RuntimeError("C1 preflight artifact harness hash does not match this run")
        if self.c1_environment is not None and (
            payload.get("c1_environment") != self.c1_environment
            or payload.get("c1_application_revisions")
            != self.c1_application_revisions
        ):
            raise RuntimeError(
                "C1 preflight environment or application revisions do not match this run"
            )
        expected = self._expected_preflight_probes()
        results = payload.get("results")
        if not isinstance(results, list) or payload.get("probe_count") != len(expected):
            raise RuntimeError("C1 preflight artifact has incomplete route coverage")
        public_results: list[dict[str, Any]] = []
        for wanted, result in zip(expected, results, strict=True):
            if not isinstance(result, dict) or any(
                result.get(field) != wanted[field]
                for field in ("name", "model", "modality")
            ):
                raise RuntimeError("C1 preflight route order or identity mismatch")
            if result.get("provider") != "relay" or result.get("success") is not True:
                raise RuntimeError("C1 preflight contains an unsuccessful provider route")
            expected_profile = self._model_request_provenance(wanted["model"])
            if result.get("request_profile") != expected_profile:
                raise RuntimeError("C1 preflight request profile mismatch")
            if not self._validate_preflight_usage(result.get("usage")):
                raise RuntimeError("C1 preflight contains invalid token usage")
            if not self._validate_preflight_attempts(
                result,
                initial_max_tokens=initial_max_tokens,
                retry_max_tokens=retry_max_tokens,
                expected_temperature=(
                    self.temperature
                    if str(wanted["name"]).startswith("decision:")
                    else None
                ),
                reasoning_disabled=(
                    expected_profile.get("profile") == "thinking_disabled"
                ),
            ):
                raise RuntimeError("C1 preflight attempt protocol is invalid")
            public_results.append(
                {
                    field: result.get(field)
                    for field in (
                        "name",
                        "model",
                        "provider",
                        "modality",
                        "success",
                        "attempts",
                        "finish_reason",
                        "usage",
                        "request_profile",
                        "request_contract_passed",
                        "reasoning_content_observed",
                        "reasoning_token_anomaly",
                        "temperature_configured",
                        "temperature_fallback_triggered",
                        "temperature_omitted_after_rejection",
                        "attempt_records",
                    )
                }
            )
        self.preflight_metadata = {
            "complete": True,
            "generated_at_utc": payload.get("generated_at_utc"),
            "artifact": str(artifact),
            "artifact_sha256": hashlib.sha256(artifact_raw).hexdigest(),
            "git_commit": payload.get("git_commit"),
            "manifest_hash": payload.get("manifest_hash"),
            "harness_hash": payload.get("harness_hash"),
            "c1_application_revisions": payload.get(
                "c1_application_revisions"
            ),
            "c1_environment": payload.get("c1_environment"),
            "api_provider": payload.get("api_provider"),
            "provider_timeout_s": payload.get("provider_timeout_s"),
            "temperature_contract": payload.get("temperature_contract"),
            "model_request_profiles": payload.get("model_request_profiles"),
            "model_request_profiles_sha256": payload.get(
                "model_request_profiles_sha256"
            ),
            "probe_count": len(public_results),
            "initial_max_tokens": initial_max_tokens,
            "retry_max_tokens": retry_max_tokens,
            "retry_rule": payload.get("retry_rule"),
            "results": public_results,
        }
        self._preflight_gate_checked = True

    def _verify_preflight_artifact_unchanged(self) -> None:
        metadata = self.preflight_metadata
        if not isinstance(metadata, dict):
            return
        raw_path = metadata.get("artifact")
        expected = metadata.get("artifact_sha256")
        if not raw_path or not expected:
            raise RuntimeError("C1 preflight provenance is incomplete")
        path = Path(str(raw_path))
        if not path.is_file():
            raise RuntimeError("C1 preflight artifact disappeared during the run")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("C1 preflight artifact changed during the run")
        migration = self.c1_source_contract_migration_metadata
        if migration is not None:
            migration_raw_path = migration.get("artifact")
            expected_migration_sha = migration.get("artifact_sha256")
            if not migration_raw_path or not expected_migration_sha:
                raise RuntimeError(
                    "C1 source contract migration provenance is incomplete"
                )
            migration_path = Path(str(migration_raw_path))
            if not migration_path.is_file():
                raise RuntimeError(
                    "C1 source contract migration artifact disappeared during the run"
                )
            if (
                hashlib.sha256(migration_path.read_bytes()).hexdigest()
                != expected_migration_sha
            ):
                raise RuntimeError(
                    "C1 source contract migration artifact changed during the run"
                )

    def _require_calibration_gate(self) -> None:
        """Block paid C-D calls until all registered background backends passed."""

        if self.agent_kind != "llm":
            return
        if self._calibration_gate_checked:
            self._verify_calibration_artifact_unchanged()
            return
        cfg = self.manifest.get("calibration") if isinstance(self.manifest.get("calibration"), dict) else {}
        if not cfg.get("required", False):
            self._calibration_gate_checked = True
            return
        if cfg.get("luanti_input_protocol") != LUANTI_TEXT_INPUT_PROTOCOL:
            raise RuntimeError(
                "Formal C1 manifest must declare the registered Luanti input "
                "calibration protocol"
            )
        artifact = self.calibration_artifact
        if artifact is None:
            env_name = str(cfg.get("artifact_env") or "WARPTEST_C1_CALIBRATION_ARTIFACT")
            raw = os.environ.get(env_name)
            artifact = Path(raw).expanduser() if raw else None
        if artifact is not None and not artifact.is_absolute():
            artifact = (self.base / artifact).resolve()
        if artifact is None or not artifact.exists():
            raise RuntimeError(
                "Formal C1 GUI run requires a passed 10-backend c1-calibration-v4 artifact"
            )
        try:
            artifact_raw = artifact.read_bytes()
            payload = json.loads(artifact_raw)
        except Exception as error:
            raise RuntimeError(f"Cannot read C1 calibration artifact: {error}") from error
        if payload.get("schema_version") != 4 or payload.get("schema") != "c1-calibration-v4":
            raise RuntimeError("C1 calibration artifact must use c1-calibration-v4")
        backends = payload.get("backends")
        registered = self.c1_background_pixel_protocol or {}
        expected_backends = registered.get("benchmarks")
        if not isinstance(backends, dict) or not isinstance(expected_backends, dict):
            raise RuntimeError("C1 calibration artifact lacks backend proofs")
        if set(backends) != set(expected_backends):
            raise RuntimeError("C1 calibration artifact backend coverage does not match manifest")
        for benchmark, identity in expected_backends.items():
            proof = backends.get(benchmark)
            if not isinstance(proof, dict) or proof.get("passed") is not True:
                raise RuntimeError(f"C1 calibration backend did not pass: {benchmark}")
            for field in ("capture_backend", "input_backend", "frame"):
                if proof.get(field) != identity.get(field):
                    raise RuntimeError(
                        f"C1 calibration {benchmark} {field} does not match manifest"
                    )
            isolation = proof.get("foreground_isolation")
            if not isinstance(isolation, dict) or not all(
                isolation.get(field) is True
                for field in (
                    "target_never_frontmost",
                    "system_pointer_unchanged",
                    "system_clipboard_unchanged",
                    "foreground_text_unchanged",
                )
            ):
                raise RuntimeError(
                    f"C1 calibration {benchmark} lacks foreground isolation proof"
                )
        if payload.get("passed") is not True:
            raise RuntimeError("C1 calibration artifact must record passed=true")
        if not self._resume_index_loaded:
            self._require_fresh_artifact(
                payload,
                max_age_hours=cfg.get("max_age_hours"),
                label="C1 calibration",
            )
        if payload.get("git_commit") != self.git_commit:
            raise RuntimeError("C1 calibration artifact git commit does not match this run")
        if payload.get("manifest_hash") != self.manifest_hash:
            raise RuntimeError("C1 calibration artifact manifest hash does not match this run")
        if payload.get("api_provider") != self._api_provider_provenance():
            raise RuntimeError("C1 calibration artifact provider routing does not match this run")
        if (
            self.provider_timeout_registered
            and payload.get("provider_timeout_s") != self.provider_timeout_s
        ):
            raise RuntimeError("C1 calibration provider timeout does not match this run")
        current_harness = self._freeze_c1_harness_hash()
        if payload.get("harness_hash") != current_harness:
            raise RuntimeError("C1 calibration artifact harness hash does not match this run")
        if self.c1_environment is not None and (
            payload.get("c1_environment") != self.c1_environment
            or payload.get("c1_application_revisions")
            != self.c1_application_revisions
        ):
            raise RuntimeError(
                "C1 calibration environment or application revisions do not match this run"
            )
        bridge = self.manifest.get("visual_bridge") if isinstance(self.manifest.get("visual_bridge"), dict) else {}
        expected_observer = str(bridge.get("observer_model") or "claude-sonnet-5@relay")
        expected_grounder = calibration_grounder_model(
            bridge.get("grounder_model"), observer_model=expected_observer
        )
        if payload.get("observer_model") != expected_observer or payload.get("grounder_model") != expected_grounder:
            raise RuntimeError("C1 calibration artifact visual bridge models do not match the manifest")
        if (
            payload.get("background_pixel_protocol_sha256")
            != self.c1_background_pixel_protocol_sha256
        ):
            raise RuntimeError(
                "C1 calibration background protocol identity does not match the run"
            )
        self.calibration_metadata = {
            "schema_version": payload.get("schema_version"),
            "schema": payload.get("schema"),
            "passed": True,
            "generated_at_utc": payload.get("generated_at_utc"),
            "artifact": str(artifact.resolve()),
            "artifact_sha256": hashlib.sha256(artifact_raw).hexdigest(),
            "git_commit": payload.get("git_commit"),
            "manifest_hash": payload.get("manifest_hash"),
            "harness_hash": payload.get("harness_hash"),
            "c1_application_revisions": payload.get(
                "c1_application_revisions"
            ),
            "c1_environment": payload.get("c1_environment"),
            "api_provider": payload.get("api_provider"),
            "provider_timeout_s": payload.get("provider_timeout_s"),
            "observer_model": payload.get("observer_model"),
            "grounder_model": payload.get("grounder_model"),
            "background_pixel_protocol_sha256": payload.get(
                "background_pixel_protocol_sha256"
            ),
            "backends": copy.deepcopy(backends),
        }
        self._calibration_gate_checked = True

    def _verify_calibration_artifact_unchanged(self) -> None:
        """Re-hash the paid calibration before every C1 invocation."""

        if not self._calibration_gate_checked:
            return
        metadata = self.calibration_metadata
        if not isinstance(metadata, dict):
            return
        raw_path = metadata.get("artifact")
        expected = metadata.get("artifact_sha256")
        if not raw_path or not expected:
            raise RuntimeError("C1 calibration provenance is incomplete")
        path = Path(str(raw_path))
        if not path.is_file():
            raise RuntimeError("C1 calibration artifact disappeared during the run")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise RuntimeError("C1 calibration artifact changed during the run")

    @staticmethod
    def _unknown_usage_snapshot(report: str | None) -> dict[str, Any] | None:
        """Extract only auditable counters from a quarantined report."""

        if not report or not Path(report).is_file():
            return None
        try:
            payload = json.loads(Path(report).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        candidates: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            if isinstance(payload.get("cost"), dict):
                candidates.append(payload["cost"])
            for result in payload.get("results") or []:
                if isinstance(result, dict) and isinstance(result.get("cost"), dict):
                    candidates.append(result["cost"])
        if not candidates:
            return None
        fields = (
            "calls",
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "reasoning_tokens_observed",
            "reasoning_tokens_lower_bound",
            "reasoning_tokens_upper_bound",
            "cached_prompt_tokens",
            "total_tokens",
            "usage_complete",
            "incomplete_calls",
            "unknown_prompt_token_calls",
            "unknown_completion_token_calls",
            "unknown_reasoning_token_calls",
            "unknown_total_token_calls",
            "reasoning_detail_complete",
            "reasoning_detail_missing_calls",
            "reasoning_detail_unknown_completion_tokens",
            "reasoning_detail_expected_completion_tokens",
            "reasoning_detail_unknown_fraction",
            "reasoning_detail_tolerance_fraction",
            "reasoning_detail_within_tolerance",
        )
        # A one-task producer normally has the full ledger at top level. Prefer
        # it over its duplicate result-level view to avoid double counting.
        chosen = candidates[0]
        snapshot = {
            field: chosen.get(field)
            for field in fields
            if field in chosen
        }
        for partition in ("by_kind", "by_model", "by_kind_model"):
            if isinstance(chosen.get(partition), dict):
                snapshot[partition] = copy.deepcopy(chosen[partition])
        return snapshot

    def _persist_c1_trial_snapshots(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        invocation_dir: Path,
        history: list[dict[str, Any]],
        invocation_error: str | None = None,
    ) -> dict[str, int]:
        """Split an incremental producer report into immutable trial evidence.

        Normal formal execution still constructs one policy/Agent-S worker per
        arm×model×seed invocation.  These derived, one-result reports change only
        the persistence/validity unit; they do not cold-start the treatment per
        trial.  Recovery roots use one-task producer calls only for tuples that
        the complete source matrix registered as noncompleted.
        """

        report_path = invocation_dir / _ARM_REPORT_FILE[arm]
        if not report_path.is_file():
            return {"ok": 0, "quarantined_unknown_usage": 0, "error": 0}
        try:
            payload = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"ok": 0, "quarantined_unknown_usage": 0, "error": 0}
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            return {"ok": 0, "quarantined_unknown_usage": 0, "error": 0}
        source_report_sha = hashlib.sha256(report_path.read_bytes()).hexdigest()
        sidecar_path = invocation_dir / _C1_INVOCATION_CONTEXT_FILE
        sidecar_raw = sidecar_path.read_bytes() if sidecar_path.is_file() else None
        counts = {"ok": 0, "quarantined_unknown_usage": 0, "error": 0}
        for result in results:
            if not isinstance(result, dict) or not result.get("task_id"):
                continue
            task_id = str(result["task_id"])
            unknown = cost.contains_unknown_usage(result)
            if unknown:
                status = "quarantined_unknown_usage"
            elif result.get("incomplete") is True:
                status = "error"
            else:
                status = "ok"
            digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
            evidence_dir = (
                self.run_dir
                / "c1-trial-evidence"
                / arm
                / run_context.manifest_hash(model or "none")
                / f"seed{seed}"
                / digest
            )
            evidence_dir.mkdir(parents=True, exist_ok=True)
            evidence_path = evidence_dir / _ARM_REPORT_FILE[arm]
            snapshot = copy.deepcopy(payload)
            snapshot["results"] = [copy.deepcopy(result)]
            snapshot["complete"] = status == "ok"
            snapshot["retryable_incomplete"] = False
            snapshot["atomic_trial_evidence"] = {
                "schema_version": C1_TRIAL_PERSISTENCE_SCHEMA,
                "mode": "shared_invocation_incremental_snapshot_v1",
                "task_id": task_id,
                "source_invocation_report": str(report_path.resolve()),
                "source_invocation_report_sha256": source_report_sha,
            }
            temporary = evidence_path.with_name(evidence_path.name + ".tmp")
            temporary.write_text(
                json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, evidence_path)
            if sidecar_raw is not None:
                evidence_sidecar = evidence_dir / _C1_INVOCATION_CONTEXT_FILE
                sidecar_temporary = evidence_sidecar.with_name(
                    evidence_sidecar.name + ".tmp"
                )
                sidecar_temporary.write_bytes(sidecar_raw)
                os.replace(sidecar_temporary, evidence_sidecar)
            error = (
                invocation_error
                if status != "ok"
                else None
            )
            self._record(
                arm,
                model=model,
                seed=seed,
                task_id=task_id,
                status=status,
                report=str(evidence_path.resolve()),
                history=history,
                error=error,
            )
            counts[status] += 1
        return counts

    def _record(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        status: str,
        report: str | None,
        history: list[dict[str, Any]],
        error: str | None = None,
        task_id: str | None = None,
        attempt: int = 0,
        replacement_for: dict[str, Any] | None = None,
        source_provenance: dict[str, Any] | None = None,
    ) -> None:
        with self._index_lock:
            report_exists = bool(report and Path(report).is_file())
            report_sha256 = (
                hashlib.sha256(Path(report).read_bytes()).hexdigest()
                if report_exists
                and status in {"ok", "quarantined_unknown_usage", "error"}
                else None
            )
            attempt_id = (
                self._c1_attempt_id(arm, model, seed, task_id, attempt)
                if task_id is not None
                else None
            )
            replacement = {
                "model": model,
                "seed": seed,
                "task_id": task_id,
                "trial_attempt": attempt if task_id is not None else None,
                "attempt_id": attempt_id,
                "status": status,
                "report": report,
                "report_sha256": report_sha256,
                "retry_history": history,
                "error": error,
                "execution": (
                    dict(self._latin_local.value)
                    if getattr(self._latin_local, "value", None)
                    else None
                ),
                "replacement_for": replacement_for,
                "source_provenance": source_provenance,
            }
            rows = self.index.setdefault(arm, [])
            rows[:] = [
                row
                for row in rows
                if not (
                    row.get("model") == model
                    and row.get("seed") == seed
                    and row.get("task_id") == task_id
                )
            ]
            rows.append(replacement)
            if status == "quarantined_unknown_usage" and task_id is not None:
                overhead = {
                    "attempt_id": attempt_id,
                    "arm": arm,
                    "model": model,
                    "seed": seed,
                    "task_id": task_id,
                    "trial_attempt": attempt,
                    "status": status,
                    "report": report,
                    "report_sha256": replacement["report_sha256"],
                    "known_and_unknown_usage": self._unknown_usage_snapshot(report),
                    "error": error,
                    "replacement_for": replacement_for,
                }
                if any(
                    row.get("attempt_id") == attempt_id
                    for row in self.c1_operational_overhead
                ):
                    raise ValueError(
                        f"duplicate C1 unknown-usage attempt evidence: {attempt_id}"
                    )
                self.c1_operational_overhead.append(overhead)
            elif (
                status == "error"
                and task_id is not None
                and isinstance(replacement_for, dict)
                and report_exists
            ):
                try:
                    report_payload = json.loads(Path(str(report)).read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    report_payload = None
                results = (
                    report_payload.get("results")
                    if isinstance(report_payload, dict)
                    else None
                )
                result = (
                    results[0]
                    if isinstance(results, list)
                    and len(results) == 1
                    and isinstance(results[0], dict)
                    else None
                )
                error_kind = (
                    self._registered_source_error_kind(
                        result=result,
                        report=report_payload,
                        source={"c1_task_timeout_s": self.c1_task_timeout_s},
                        task_id=task_id,
                    )
                    if isinstance(result, dict)
                    else None
                )
                if error_kind is not None:
                    if any(
                        row.get("attempt_id") == attempt_id
                        for row in self.c1_operational_overhead
                    ):
                        raise ValueError(
                            f"duplicate C1 exact-usage attempt evidence: {attempt_id}"
                        )
                    self.c1_operational_overhead.append(
                        {
                            "attempt_id": attempt_id,
                            "arm": arm,
                            "model": model,
                            "seed": seed,
                            "task_id": task_id,
                            "trial_attempt": attempt,
                            "status": error_kind,
                            "report": report,
                            "report_sha256": report_sha256,
                            "known_and_unknown_usage": self._unknown_usage_snapshot(
                                report
                            ),
                            "error": error,
                            "reason": result.get("reason"),
                            "replacement_for": replacement_for,
                        }
                    )
            self._write_index_unlocked()

    def _execute(
        self,
        arm: str,
        *,
        model: str | None,
        seed: int | None,
        fn: Callable[[dict[str, Any]], dict[str, Any]],
        invocation_dir: Path | None = None,
        tasks_path: str | Path | None = None,
        invocation: dict[str, Any] | None = None,
        task_id: str | None = None,
        attempt: int = 0,
        replacement_for: dict[str, Any] | None = None,
    ) -> str | None:
        context_guard: tuple[Path, dict[str, Any]] | None = None
        c2_context_guard: tuple[Path, dict[str, Any]] | None = None
        if arm in C1_EXECUTION_ARMS:
            self._require_preflight_gate()
            if invocation_dir is None or tasks_path is None:
                raise ValueError("C1 execution requires invocation_dir and tasks_path provenance")
            context_guard = self._prepare_c1_invocation(
                arm,
                model=model,
                seed=seed,
                invocation_dir=invocation_dir,
                tasks_path=tasks_path,
                invocation=invocation,
                task_id=task_id,
                attempt=attempt,
            )
        elif arm == "c2_memory_baseline":
            if invocation_dir is None:
                raise ValueError("C2 execution requires invocation_dir provenance")
            c2_context_guard = self._prepare_c2_invocation(
                model=model,
                seed=seed,
                invocation_dir=invocation_dir,
                invocation=invocation,
            )
        if self._completed(arm, model=model, seed=seed, task_id=task_id):
            print(
                f"[p5] resume skip {arm} model={model} seed={seed}"
                + (f" task={task_id}" if task_id else ""),
                flush=True,
            )
            return
        run_meta = self._run_meta(
            arm, model=model, seed=seed, task_id=task_id, attempt=attempt
        )
        label = f"{arm} model={model} seed={seed}" + (
            f" task={task_id} attempt={attempt}" if task_id else ""
        )
        print(f"[p5] start {label}", flush=True)
        try:
            def invoke() -> dict[str, Any]:
                if context_guard is not None:
                    self._validate_c1_invocation_context(*context_guard)
                    assert invocation_dir is not None
                    prior_report = invocation_dir / _ARM_REPORT_FILE[arm]
                    if prior_report.is_file():
                        try:
                            prior_payload = json.loads(
                                prior_report.read_text(encoding="utf-8")
                            )
                        except (OSError, json.JSONDecodeError):
                            prior_payload = None
                        if cost.contains_unknown_usage(prior_payload):
                            raise cost.UsageAccountingError(
                                "refusing same-directory C1 resume because the prior "
                                "report contains permanently unknown provider usage"
                            )
                if c2_context_guard is not None:
                    expected_c2 = self._c2_invocation_context(
                        model=model,
                        seed=seed,
                        invocation=invocation,
                    )
                    self._validate_c2_invocation_context(
                        c2_context_guard[0], expected_c2
                    )
                result = fn(run_meta)
                if context_guard is not None and tasks_path is not None:
                    refreshed = self._c1_invocation_context(
                        arm,
                        model=model,
                        seed=seed,
                        tasks_path=tasks_path,
                        invocation=invocation,
                        task_id=task_id,
                        attempt=attempt,
                    )
                    self._validate_c1_invocation_context(
                        context_guard[0], refreshed
                    )
                if c2_context_guard is not None:
                    refreshed_c2 = self._c2_invocation_context(
                        model=model,
                        seed=seed,
                        invocation=invocation,
                    )
                    self._validate_c2_invocation_context(
                        c2_context_guard[0], refreshed_c2
                    )
                if arm in C1_EXECUTION_ARMS and result.get("complete") is not True:
                    if cost.contains_unknown_usage(result):
                        raise cost.UsageAccountingError(
                            "C1 producer returned permanently unknown provider usage; "
                            "automatic retry and same-directory resume are forbidden"
                        )
                    if result.get("retryable_incomplete") is True:
                        raise IncompleteC1InvocationError(
                            "temporarily unavailable: C1 producer returned an explicitly "
                            "retryable infrastructure report"
                        )
                    raise NonRetryableC1InvocationError(
                        "C1 producer returned nonretryable incomplete evidence; "
                        "automatic stochastic replay is forbidden"
                    )
                return result

            try:
                if isinstance(invocation, dict) and invocation.get(
                    "atomic_defer"
                ) is True:
                    result = invoke()
                    history = []
                else:
                    result, history = run_context.call_with_retries(
                        invoke,
                        label=label,
                        max_attempts=self.max_attempts,
                        base_sleep_s=self.base_sleep_s,
                        max_sleep_s=self.max_sleep_s,
                        on_event=lambda msg: print(f"[p5] {msg}", flush=True),
                    )
            except C1TaskDeferred:
                if context_guard is not None and invocation_dir is not None:
                    context_path = context_guard[0]
                    report_path = invocation_dir / _ARM_REPORT_FILE[arm]
                    remaining = (
                        sorted(invocation_dir.iterdir())
                        if invocation_dir.is_dir()
                        else []
                    )
                    if report_path.exists() or remaining != [context_path]:
                        raise ValueError(
                            "deferred C1 invocation left unexpected attempt artifacts"
                        ) from None
                    context_path.unlink()
                    invocation_dir.rmdir()
                return "deferred"
            except C1TaskDispatchCancelled:
                if isinstance(invocation, dict) and invocation.get(
                    "atomic_defer"
                ) is True:
                    return "cancelled"
                raise
        except BaseException as error:  # noqa: BLE001 - always persist failure; C1 pauses below
            history = getattr(error, "_p5_retry_history", [])
            error_text = run_context.safe_error_text(error)
            if (
                arm in C1_EXECUTION_ARMS
                and self.c1_trial_persistence is not None
                and task_id is None
                and invocation_dir is not None
            ):
                _patch_retry_history(
                    invocation_dir, _ARM_REPORT_FILE[arm], history
                )
                counts = self._persist_c1_trial_snapshots(
                    arm,
                    model=model,
                    seed=seed,
                    invocation_dir=invocation_dir,
                    history=history,
                    invocation_error=error_text,
                )
                if isinstance(error, cost.UsageAccountingError):
                    if counts["quarantined_unknown_usage"] == 0:
                        # Without a task-bearing incremental report there is no
                        # defensible replacement tuple. Preserve the incident and
                        # make the source index deliberately non-importable.
                        with self._index_lock:
                            self.c1_operational_overhead.append(
                                {
                                    "arm": arm,
                                    "model": model,
                                    "seed": seed,
                                    "task_id": None,
                                    "status": "unassigned_unknown_usage",
                                    "known_and_unknown_usage": None,
                                    "error": error_text,
                                }
                            )
                            self._write_index_unlocked()
                    self._c1_quarantined.append(
                        {
                            "arm": arm,
                            "model": model,
                            "seed": seed,
                            "task_id": None,
                        }
                    )
                print(f"[p5] FAILED {label}: {error_text}", flush=True)
                raise
            failed_report = None
            if arm in C1_EXECUTION_ARMS and invocation_dir is not None:
                candidate = invocation_dir / _ARM_REPORT_FILE[arm]
                if candidate.is_file():
                    failed_report = str(candidate.resolve())
            quarantined = bool(
                arm in C1_EXECUTION_ARMS
                and task_id is not None
                and isinstance(error, cost.UsageAccountingError)
            )
            status = "quarantined_unknown_usage" if quarantined else "error"
            self._record(
                arm,
                model=model,
                seed=seed,
                task_id=task_id,
                attempt=attempt,
                status=status,
                report=failed_report,
                history=history,
                error=error_text,
                replacement_for=replacement_for,
            )
            if quarantined:
                self._c1_quarantined.append(
                    {
                        "arm": arm,
                        "model": model,
                        "seed": seed,
                        "task_id": task_id,
                    }
                )
            print(f"[p5] FAILED {label}: {error_text}", flush=True)
            if arm in C1_EXECUTION_ARMS:
                raise
            return
        report_dir = result.get("report_dir")
        run_context_history = run_meta.get("retry_history") or []
        full_history = (run_context_history + history) if run_context_history else history
        _patch_retry_history(report_dir, _ARM_REPORT_FILE[arm], full_history)
        report_path = (
            str((Path(report_dir) / _ARM_REPORT_FILE[arm]).resolve())
            if report_dir
            else None
        )
        if report_path is None or not Path(report_path).is_file():
            self._record(
                arm,
                model=model,
                seed=seed,
                task_id=task_id,
                attempt=attempt,
                status="error",
                report=None,
                history=full_history,
                error="producer did not persist its declared report",
            )
            raise RuntimeError("C1 producer did not persist its declared report")
        if arm == "c2_memory_baseline":
            evidence_issue = _c2_memory_report_issue(Path(report_path))
            if evidence_issue is not None:
                self._record(
                    arm,
                    model=model,
                    seed=seed,
                    status="error",
                    report=None,
                    history=full_history,
                    error=evidence_issue,
                )
                print(f"[p5] INVALID {label}: {evidence_issue}", flush=True)
                return
        if (
            arm in C1_EXECUTION_ARMS
            and self.c1_trial_persistence is not None
            and task_id is None
            and invocation_dir is not None
        ):
            counts = self._persist_c1_trial_snapshots(
                arm,
                model=model,
                seed=seed,
                invocation_dir=invocation_dir,
                history=full_history,
            )
            if counts["ok"] == 0 or any(
                counts[field]
                for field in ("quarantined_unknown_usage", "error")
            ):
                raise RuntimeError(
                    "complete C1 producer report did not yield exact trial snapshots"
                )
            print(
                f"[p5] ok {label} -> {counts['ok']} atomic trial snapshots",
                flush=True,
            )
            return
        self._record(
            arm,
            model=model,
            seed=seed,
            task_id=task_id,
            attempt=attempt,
            status="ok",
            report=report_path,
            history=full_history,
            replacement_for=replacement_for,
        )
        print(f"[p5] ok {label} -> {report_path}", flush=True)

    def _arm_cfg(self, arm: str) -> dict[str, Any]:
        return self.manifest.get("arms", {}).get(arm, {}) or {}

    def _synthesis_budget(self, arm: str) -> dict[str, Any]:
        cfg = self._arm_cfg(arm)
        return {
            # The GUI arms always issue the shared constant; only the state arm
            # reads this from its manifest block, so compare effective values.
            "explore": (
                bool(cfg.get("explore", C1_SYNTHESIS_EXPLORE))
                if arm == "c1_state"
                else C1_SYNTHESIS_EXPLORE
            ),
            "max_explore_steps": int(cfg.get("max_explore_steps", 12)),
            "tasks": str(cfg.get("tasks", "benchmarks/p5/c1_tasks.yaml")),
        }

    def _require_shared_synthesis_parity(self, selected_arms: list[str]) -> None:
        """Fail closed when the self-synthesizing arms disagree on the budget.

        These arms read one checkpoint cache whose key includes the exploration
        budget, so a manifest that gives them different values does not produce a
        comparison -- it silently produces two different starts per task. The
        budget is also quoted verbatim into the synthesis prompt.
        """

        if "c1_state_minimal_agent" in selected_arms:
            diagnostic = self._arm_cfg("c1_state_minimal_agent")
            if diagnostic.get("transition_action_source") == "source_agent_v2":
                state_budget = self._synthesis_budget("c1_state")
                diagnostic_budget = self._synthesis_budget(
                    "c1_state_minimal_agent"
                )
                source_steps = int(
                    diagnostic.get("source_action_max_steps", 0) or 0
                )
                start_steps = int(
                    diagnostic.get("max_explore_steps", 0) or 0
                )
                source_tokens = int(
                    diagnostic.get("source_action_max_tokens", 0) or 0
                )
                repair_count = int(
                    diagnostic.get("source_action_format_repairs", -1)
                )
                prompt_revision = diagnostic.get(
                    "source_action_prompt_revision"
                )
                pair_protocol = diagnostic.get("pair_start_protocol")
                if (
                    diagnostic_budget != state_budget
                    or source_steps != start_steps
                    or source_steps != int(state_budget["max_explore_steps"])
                    or source_tokens != int(C1_SYNTHESIS_MAX_TOKENS)
                    or repair_count != 1
                    or prompt_revision != "c1-source-transition-v2"
                    or pair_protocol != "c1-paired-start-gate-v1"
                ):
                    raise RuntimeError(
                        "source_agent_v2 must match the registered E/source/pair "
                        "protocol: "
                        f"steps={state_budget['max_explore_steps']}, "
                        f"max_tokens={C1_SYNTHESIS_MAX_TOKENS}, "
                        "format_repairs=1, prompt=c1-source-transition-v2, "
                        "pair_start=c1-paired-start-gate-v1"
                    )
        present = [
            arm
            for arm in C1_EXECUTION_SELF_SYNTHESIZING_ARMS
            if arm in selected_arms
        ]
        if len(present) < 2:
            return
        reference = self._synthesis_budget(present[0])
        for arm in present[1:]:
            budget = self._synthesis_budget(arm)
            if budget != reference:
                differing = sorted(
                    field
                    for field in reference
                    if budget.get(field) != reference.get(field)
                )
                raise RuntimeError(
                    "Self-synthesizing C1 arms must share one synthesis budget; "
                    f"{present[0]} and {arm} differ on {differing}"
                )

    def _c1_tasks_path(self, cfg: dict[str, Any]) -> str | Path:
        source = cfg.get("tasks", "benchmarks/p5/c1_tasks.yaml")
        if not self.task_ids:
            return source
        if self._selected_tasks_path is None:
            selected = self.run_dir / "selected-c1-tasks.yaml"
            if self.resume and selected.exists():
                self._selected_tasks_path = selected
            else:
                self._selected_tasks_path = write_subset_tasks(
                    self.base,
                    source,
                    self.task_ids,
                    selected,
                )
        return self._selected_tasks_path

    def _c1_task_records(self, cfg: dict[str, Any]) -> list[dict[str, Any]]:
        """Load the run's registered tasks in their already-fixed order."""

        path = Path(self._c1_tasks_path(cfg)).expanduser()
        if not path.is_absolute():
            path = (self.base / path).resolve()
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        if not isinstance(tasks, list) or not tasks:
            raise ValueError(f"C1 task file must contain tasks: {path}")
        records = [dict(task) for task in tasks if isinstance(task, dict)]
        ids = [str(task.get("id") or "") for task in records]
        if len(records) != len(tasks) or any(not task_id for task_id in ids):
            raise ValueError(f"C1 task file contains a malformed task: {path}")
        if len(ids) != len(set(ids)):
            raise ValueError(f"C1 task file contains duplicate task ids: {path}")
        return records

    def _c1_inner_lane_plan(
        self, records: list[dict[str, Any]]
    ) -> list[list[dict[str, Any]]]:
        tasks = [
            SimpleNamespace(
                id=str(record["id"]),
                benchmark=str(record.get("benchmark") or "unknown"),
                horizon_class=str(record.get("horizon_class") or "mid"),
                repo=str(record.get("repo") or "."),
            )
            for record in records
        ]
        lanes = build_lane_plan(
            tasks,
            workers=self.c1_inner_worker,
            repo_for_task=lambda task: (
                Path(task.repo).expanduser().resolve()
                if Path(task.repo).is_absolute()
                else (self.base / task.repo).resolve()
            ),
        )
        return [[assignment.metadata() for assignment in lane] for lane in lanes]

    def _c1_inner_task_plan(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        return [
            item.metadata()
            for item in self._c1_ready_registrations(records)
        ]

    def _c1_ready_registrations(
        self, records: list[dict[str, Any]]
    ) -> list[Any]:
        tasks = [
            SimpleNamespace(
                id=str(record["id"]),
                benchmark=str(record.get("benchmark") or "unknown"),
                horizon_class=str(record.get("horizon_class") or "mid"),
                repo=str(record.get("repo") or "."),
            )
            for record in records
        ]
        return build_ready_task_plan(
            tasks,
            repo_for_task=lambda task: (
                Path(task.repo).expanduser().resolve()
                if Path(task.repo).is_absolute()
                else (self.base / task.repo).resolve()
            ),
        )

    def _run_c1_recovery_invocation(
        self,
        *,
        invocation_id: str,
        recovery_tasks: list[dict[str, Any]],
        execute_task: Callable[[dict[str, Any], Any, int, Any], str | None],
    ) -> None:
        """Run atomic recovery tuples with the normal C1 inner-lane policy."""

        if not recovery_tasks:
            return
        lane_count = min(self.c1_inner_worker, len(recovery_tasks))
        active_latin = copy.deepcopy(
            getattr(self._latin_local, "value", None)
        )
        recovery_handle = self.c1_ready_scheduler.register_invocation(
            invocation_id,
            outer_ordinal=int(
                (active_latin or {}).get("outer_ordinal", 0)
            ),
            registrations=self._c1_ready_registrations(recovery_tasks),
            resources_for_task=lambda task: c1_task_logical_resources(
                task.benchmark,
                unity_serialized_benchmarks=(
                    self.c1_unity_serialized_benchmarks
                ),
            ),
            worker_limit=lane_count,
        )
        task_by_id = {
            str(task["id"]): task for task in recovery_tasks
        }
        failure_lock = threading.Lock()
        failures: list[BaseException] = []

        def remember_failure(error: BaseException) -> None:
            with failure_lock:
                if not failures:
                    failures.append(error)

        def run_lane(lane_id: int) -> None:
            ticket = None
            prior_latin = getattr(self._latin_local, "value", None)
            self._latin_local.value = copy.deepcopy(active_latin)
            try:
                while True:
                    if (
                        self._c1_global_stop_event is not None
                        and self._c1_global_stop_event.is_set()
                    ):
                        recovery_handle.close_pending_dispatch(
                            reason="global C1 stop requested"
                        )
                        return
                    ticket = recovery_handle.claim(lane_id=lane_id)
                    if ticket is None:
                        return
                    try:
                        outcome = execute_task(
                            task_by_id[ticket.task_id],
                            ticket,
                            lane_id,
                            recovery_handle,
                        )
                    except C1TaskDeferred:
                        if ticket.state == "claimed":
                            raise RuntimeError(
                                "deferred C1 recovery ticket was not requeued"
                            ) from None
                        ticket = None
                        continue
                    if outcome == "deferred":
                        if ticket.state == "claimed":
                            raise RuntimeError(
                                "deferred C1 recovery ticket was not requeued"
                            )
                        ticket = None
                        continue
                    if outcome == "cancelled":
                        if ticket.state == "claimed":
                            recovery_handle.complete(ticket, status="cancelled")
                        recovery_handle.close_pending_dispatch(
                            reason="global C1 stop requested"
                        )
                        ticket = None
                        return
                    if ticket.state == "claimed":
                        # Test doubles may omit the producer's normal admit/complete
                        # cycle. Never strand their logical resource claims.
                        recovery_handle.complete(ticket)
                    ticket = None
            except BaseException as error:  # noqa: BLE001 - settle sibling lanes
                recovery_handle.close_pending_dispatch(
                    reason=run_context.safe_error_text(error)
                )
                if ticket is not None and ticket.state in {"claimed", "running"}:
                    try:
                        recovery_handle.complete(ticket, status="error")
                    except ValueError:
                        pass
                remember_failure(error)
            finally:
                self._latin_local.value = prior_latin

        with ThreadPoolExecutor(max_workers=lane_count) as pool:
            futures = [
                pool.submit(run_lane, lane_id)
                for lane_id in range(lane_count)
            ]
            for future in futures:
                future.result()
        if failures:
            raise failures[0]

    def _c1_trial_tasks_path(
        self, cfg: dict[str, Any], task: dict[str, Any]
    ) -> Path:
        """Materialize one immutable task file for a trial-atomic producer call."""

        task_id = str(task.get("id") or "")
        if not task_id:
            raise ValueError("atomic C1 task is missing id")
        with self._trial_task_paths_lock:
            cached = self._trial_task_paths.get(task_id)
            if cached is not None:
                return cached
            source_path = Path(self._c1_tasks_path(cfg)).expanduser()
            if not source_path.is_absolute():
                source_path = (self.base / source_path).resolve()
            source_payload = yaml.safe_load(source_path.read_text(encoding="utf-8"))
            if not isinstance(source_payload, dict):
                raise ValueError(f"C1 task file must be a mapping: {source_path}")
            canonical_source = source_payload.get("source_task_file") or str(source_path)
            payload = {
                **source_payload,
                "tasks": [copy.deepcopy(task)],
                "source_task_file": str(canonical_source),
                "selected_task_ids": [task_id],
                "trial_atomic": True,
            }
            digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
            path = self.run_dir / "c1-trial-tasks" / f"{digest}.yaml"
            path.parent.mkdir(parents=True, exist_ok=True)
            encoded = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True)
            if path.exists():
                if path.read_text(encoding="utf-8") != encoded:
                    raise ValueError(
                        f"atomic C1 task file changed inside the run: {task_id}"
                    )
            else:
                temporary = path.with_name(path.name + ".tmp")
                temporary.write_text(encoded, encoding="utf-8")
                os.replace(temporary, path)
            self._trial_task_paths[task_id] = path
            return path

    @staticmethod
    def _trial_key(
        arm: str, model: str | None, seed: int | None, task_id: str
    ) -> TrialKey:
        return TrialKey(arm, model, seed, task_id)

    def _c1_attempt_id(
        self,
        arm: str,
        model: str | None,
        seed: int | None,
        task_id: str,
        attempt: int,
    ) -> str:
        return (
            f"{self.run_id}:{arm}:{model or 'na'}:"
            f"{seed if seed is not None else 'na'}:{task_id}:attempt{attempt}"
        )

    def _recovery_trial_attempt(
        self, arm: str, *, model: str, seed: int, task_id: str
    ) -> int | None:
        """Select one pre-registered recovery attempt without implicit retries."""

        if self.c1_source_index is None or self.c1_trial_persistence is None:
            raise ValueError("recovery attempt selection requires a registered source")
        matches = [
            row
            for row in self.index.get(arm, [])
            if row.get("model") == model
            and row.get("seed") == seed
            and row.get("task_id") == task_id
        ]
        if len(matches) != 1:
            raise ValueError("recovery root has a missing or duplicate local tuple")
        row = matches[0]
        status = row.get("status")
        if status == "ok":
            return None
        if status == "pending":
            if row.get("trial_attempt") is not None or row.get("attempt_id") is not None:
                raise ValueError("pending recovery tuple already claims an attempt")
            return 0
        if status not in {"quarantined_unknown_usage", "error"}:
            raise ValueError(f"recovery tuple has non-resumable status: {status}")
        if not self.resume:
            raise QuarantinedC1TrialsError(
                "a noncompleted recovery attempt requires a new explicit --resume invocation; "
                "no policy call was made"
            )
        prior_attempt = row.get("trial_attempt")
        if not isinstance(prior_attempt, int) or isinstance(prior_attempt, bool):
            raise ValueError("noncompleted recovery tuple has an invalid attempt ordinal")
        if status == "error" and not any(
            item.get("arm") == arm
            and item.get("model") == model
            and item.get("seed") == seed
            and item.get("task_id") == task_id
            and item.get("trial_attempt") == prior_attempt
            and item.get("source_provenance") is None
            for item in self.c1_operational_overhead
        ):
            # The current report is a known-usage, nonretryable terminal model
            # outcome.  It is settled evidence, not a registered retry marker.
            return None
        next_attempt = prior_attempt + 1
        maximum = int(self.c1_trial_persistence["max_recovery_attempts_per_tuple"])
        migration = getattr(self, "c1_source_contract_migration_metadata", None) or {}
        if migration.get("migration_id") == "c1_source_action_sidecar_validation_v1":
            for consumed in migration["consumed_recovery_attempts"]:
                if (consumed["arm"], consumed["model"], consumed["seed"], consumed["task_id"]) == (arm, model, seed, task_id):
                    maximum -= consumed["attempt_count"]
        if next_attempt >= maximum:
            # This tuple is already in an auditable terminal state.  Do not let
            # it prevent other pending tuples in the same invocation from
            # consuming their registered recovery attempts.
            return None
        return next_attempt

    def _c1_trial_dir(
        self, arm: str, model: str, seed: int, task_id: str, *, attempt: int = 0
    ) -> Path:
        digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]
        return (
            self.run_dir
            / arm
            / run_context.manifest_hash(model)
            / f"seed{seed}"
            / "trials"
            / digest
            / f"attempt{attempt}"
        )

    def _register_c1_trial_matrix(self, selected_arms: list[str]) -> None:
        """Persist the complete tuple universe before the first paid C1 call."""

        if self.c1_trial_persistence is None:
            return
        task_ids: list[str] | None = None
        for arm in selected_arms:
            ids = [str(task["id"]) for task in self._c1_task_records(self._arm_cfg(arm))]
            if task_ids is None:
                task_ids = ids
            elif ids != task_ids:
                raise ValueError("trial-atomic C1 arms must use one identical ordered task set")
        assert task_ids is not None
        with self._index_lock:
            for arm in selected_arms:
                rows = self.index.setdefault(arm, [])
                existing = {
                    (row.get("model"), row.get("seed"), row.get("task_id"))
                    for row in rows
                    if isinstance(row, dict)
                }
                for model in self.models:
                    for seed in range(self.seeds):
                        for task_id in task_ids:
                            key = (model, seed, task_id)
                            if key in existing:
                                continue
                            rows.append(
                                {
                                    "model": model,
                                    "seed": seed,
                                    "task_id": task_id,
                                    "trial_attempt": None,
                                    "attempt_id": None,
                                    "status": "pending",
                                    "report": None,
                                    "report_sha256": None,
                                    "retry_history": [],
                                    "error": None,
                                    "execution": None,
                                    "replacement_for": None,
                                    "source_provenance": None,
                                }
                            )
            self._write_index_unlocked()

    @staticmethod
    def _source_gate_provenance(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        return {key: item for key, item in value.items() if key != "artifact"}

    @staticmethod
    def _preflight_scientific_contract(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            field: value.get(field)
            for field in (
                "complete",
                "git_commit",
                "manifest_hash",
                "harness_hash",
                "c1_application_revisions",
                "c1_environment",
                "api_provider",
                "provider_timeout_s",
                "model_request_profiles",
                "model_request_profiles_sha256",
                "probe_count",
                "initial_max_tokens",
                "retry_max_tokens",
                "retry_rule",
                "temperature_contract",
            )
        }

    @staticmethod
    def _calibration_scientific_contract(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        browser = value.get("browser") if isinstance(value.get("browser"), dict) else {}
        luanti = value.get("luanti") if isinstance(value.get("luanti"), dict) else {}
        native_session = (
            value.get("native_session")
            if isinstance(value.get("native_session"), dict)
            else {}
        )
        text_input = (
            luanti.get("text_input")
            if isinstance(luanti.get("text_input"), dict)
            else {}
        )
        return {
            "schema_version": value.get("schema_version"),
            "passed": value.get("passed"),
            "git_commit": value.get("git_commit"),
            "manifest_hash": value.get("manifest_hash"),
            "harness_hash": value.get("harness_hash"),
            "c1_application_revisions": value.get(
                "c1_application_revisions"
            ),
            "c1_environment": value.get("c1_environment"),
            "api_provider": value.get("api_provider"),
            "provider_timeout_s": value.get("provider_timeout_s"),
            "observer_model": value.get("observer_model"),
            "grounder_model": value.get("grounder_model"),
            "keyboard_input_sources_sha256": native_session.get(
                "keyboard_input_sources_sha256"
            ),
            "browser": {
                "passed": browser.get("passed"),
                "frame": browser.get("frame"),
                "headless": browser.get("headless"),
            },
            "luanti": {
                "passed": luanti.get("passed"),
                "frame": luanti.get("frame"),
                "worldmod_click_event": luanti.get("worldmod_click_event"),
                "text_input_protocol": text_input.get("protocol"),
                "text_input_transport": text_input.get("transport"),
            },
        }

    @staticmethod
    def _registered_source_error_kind(
        *,
        result: dict[str, Any],
        report: dict[str, Any],
        source: dict[str, Any],
        task_id: str,
    ) -> str | None:
        """Classify only exact-usage source errors registered for replacement."""

        actual_cost = result.get("actual_cost")
        if not isinstance(actual_cost, dict):
            actual_cost = result.get("cost")
        exact_usage = bool(
            isinstance(actual_cost, dict)
            and actual_cost.get("usage_complete") is True
            and all(
                actual_cost.get(field) == 0
                for field in (
                    "unknown_prompt_token_calls",
                    "unknown_completion_token_calls",
                    "unknown_total_token_calls",
                )
            )
        )
        if (
            not exact_usage
            or result.get("incomplete") is not True
            or report.get("complete") is not False
            or not isinstance(report.get("retryable_incomplete"), bool)
        ):
            return None
        timeout_s = source.get("c1_task_timeout_s")
        expected_timeout_reason = (
            f"C1 task {task_id} exceeded its {timeout_s:g}s wall-clock limit"
            if isinstance(timeout_s, (int, float))
            else ""
        )
        if (
            result.get("timed_out") is True
            and result.get("infrastructure_error") is False
            and result.get("retryable_incomplete") is False
            and report.get("retryable_incomplete") is False
            and str(result.get("reason") or "") == expected_timeout_reason
        ):
            return "exact_usage_task_timeout"
        attempts = result.get("attempt_costs")
        latest_attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
        if (
            result.get("timed_out") is not True
            and (
                result.get("infrastructure_error") is True
                or result.get("infrastructure_error") is None
            )
            and result.get("retryable_incomplete") is True
            and isinstance(latest_attempt, dict)
            and latest_attempt.get("attempt_status") == "retryable_infrastructure"
            and latest_attempt.get("retry_category") in {"infra", "rate_limit"}
            and isinstance(latest_attempt.get("retry_reason"), str)
            and bool(latest_attempt["retry_reason"].strip())
            and isinstance(result.get("reason"), str)
            and bool(result["reason"].strip())
        ):
            return "exact_usage_retryable_infrastructure"
        return None

    @staticmethod
    def _registered_source_model_failure_kind(
        *,
        result: dict[str, Any],
        report: dict[str, Any],
    ) -> str | None:
        """Recognize exact-usage, nonretryable model outcomes for preservation."""

        actual_cost = result.get("actual_cost")
        if not isinstance(actual_cost, dict):
            actual_cost = result.get("cost")
        if not (
            isinstance(actual_cost, dict)
            and actual_cost.get("usage_complete") is True
            and all(
                actual_cost.get(field) == 0
                for field in (
                    "unknown_prompt_token_calls",
                    "unknown_completion_token_calls",
                    "unknown_total_token_calls",
                )
            )
            and result.get("incomplete") is True
            and report.get("complete") is False
        ):
            return None
        attempts = result.get("attempt_costs")
        latest_attempt = attempts[-1] if isinstance(attempts, list) and attempts else None
        if (
            result.get("retryable_incomplete") is True
            and result.get("infrastructure_error") in {True, None}
            and report.get("retryable_incomplete") is True
            and isinstance(latest_attempt, dict)
            and latest_attempt.get("attempt_status") == "retryable_infrastructure"
            and latest_attempt.get("retry_category") == "infra"
            and isinstance(latest_attempt.get("retry_reason"), str)
            and bool(latest_attempt["retry_reason"].strip())
        ):
            return "exact_usage_exhausted_retryable_infrastructure"
        explicit_nonretryable = bool(
            result.get("retryable_incomplete") is False
            and result.get("infrastructure_error") is False
            and report.get("retryable_incomplete") is False
            and isinstance(result.get("reason"), str)
            and bool(result["reason"].strip())
        )
        ledger_nonretryable = bool(
            result.get("retryable_incomplete") is False
            and result.get("infrastructure_error") in {False, None}
            and result.get("timed_out") is not True
            and report.get("retryable_incomplete") is False
            and report.get("incomplete_category")
            == "nonretryable_incomplete_evidence"
            and isinstance(latest_attempt, dict)
            and latest_attempt.get("attempt_status") == "nonretryable_incomplete"
            and latest_attempt.get("retry_category") is None
            and latest_attempt.get("retry_reason") is None
        )
        if not (explicit_nonretryable or ledger_nonretryable):
            return None
        return (
            "exact_usage_nonretryable_task_timeout"
            if result.get("timed_out") is True
            else "exact_usage_nonretryable_model_failure"
        )

    @staticmethod
    def _validate_source_trial_artifacts(
        *,
        row: dict[str, Any],
        source: dict[str, Any],
        source_index_path: Path,
        source_status: str,
        allow_nonretryable_model_failure: bool = False,
        allow_registered_harness_failure: bool = False,
    ) -> tuple[Path, str, str, dict[str, Any]]:
        raw_report = row.get("report")
        if not isinstance(raw_report, str) or not raw_report:
            raise ValueError("C1 source trial is missing its report path")
        report_path = Path(raw_report).expanduser()
        if not report_path.is_absolute():
            report_path = (source_index_path.parent / report_path).resolve()
        if not report_path.is_file():
            raise ValueError(f"C1 source trial report is missing: {report_path}")
        report_raw = report_path.read_bytes()
        report_sha = hashlib.sha256(report_raw).hexdigest()
        if (
            row.get("report_sha256") != report_sha
            and not (
                source_status == "error"
                and row.get("report_sha256") is None
            )
        ):
            raise ValueError("C1 source trial report_sha256 mismatch")
        try:
            report = json.loads(report_raw)
        except json.JSONDecodeError as error:
            raise ValueError("C1 source trial report is unreadable") from error
        if not isinstance(report, dict):
            raise ValueError("C1 source trial report must be an object")
        contains_unknown = cost.contains_unknown_usage(report)
        if source_status == "quarantined_unknown_usage":
            if not contains_unknown or report.get("complete") is True:
                raise ValueError(
                    "quarantined C1 source trial must contain unknown usage and be incomplete"
                )
        elif source_status == "ok":
            if contains_unknown or report.get("complete") is not True:
                raise ValueError(
                    "completed C1 source trial must be complete with exact usage"
                )
        elif source_status == "error":
            if contains_unknown or report.get("complete") is not False:
                raise ValueError(
                    "replaceable C1 source error must be incomplete with exact usage"
                )
        else:
            raise ValueError(f"unsupported C1 source trial status: {source_status}")
        results = report.get("results")
        task_id = str(row.get("task_id") or "")
        if (
            not isinstance(results, list)
            or len(results) != 1
            or not isinstance(results[0], dict)
            or str(results[0].get("task_id") or "") != task_id
        ):
            raise ValueError("C1 source report is not one atomic matching trial")
        result = results[0]
        if source_status == "error":
            replaceable_kind = P5Orchestrator._registered_source_error_kind(
                result=result,
                report=report,
                source=source,
                task_id=task_id,
            )
            preserved_kind = P5Orchestrator._registered_source_model_failure_kind(
                result=result,
                report=report,
            )
            actual_cost = result.get("actual_cost")
            if not isinstance(actual_cost, dict):
                actual_cost = result.get("cost")
            registered_harness_failure = bool(
                allow_registered_harness_failure
                and result.get("incomplete") is True
                and isinstance(actual_cost, dict)
                and actual_cost.get("usage_complete") is True
                and all(
                    actual_cost.get(field) == 0
                    for field in (
                        "unknown_prompt_token_calls",
                        "unknown_completion_token_calls",
                        "unknown_total_token_calls",
                    )
                )
            )
            if replaceable_kind is None and not (
                allow_nonretryable_model_failure and preserved_kind is not None
            ) and not registered_harness_failure:
                raise ValueError(
                    "C1 source error is not a registered exact-usage timeout "
                    "or retryable infrastructure failure"
                )
        atomic_evidence = report.get("atomic_trial_evidence")
        shared_snapshot = bool(
            isinstance(atomic_evidence, dict)
            and atomic_evidence.get("schema_version")
            == C1_TRIAL_PERSISTENCE_SCHEMA
            and atomic_evidence.get("mode")
            == "shared_invocation_incremental_snapshot_v1"
            and atomic_evidence.get("task_id") == task_id
        )
        recovery_snapshot = bool(
            not shared_snapshot
            and isinstance(row.get("replacement_for"), dict)
            and isinstance(row.get("trial_attempt"), int)
            and not isinstance(row.get("trial_attempt"), bool)
            and row.get("attempt_id")
            == (
                f"{source.get('run_id')}:{row.get('arm')}:"
                f"{row.get('model') or 'na'}:"
                f"{row.get('seed') if row.get('seed') is not None else 'na'}:"
                f"{task_id}:attempt{row.get('trial_attempt')}"
            )
        )
        if not shared_snapshot and not recovery_snapshot:
            raise ValueError(
                "C1 source completed/quarantined tuple lacks registered "
                "shared-invocation or recovery-trial evidence"
            )
        if shared_snapshot:
            source_invocation = Path(
                str(atomic_evidence.get("source_invocation_report") or "")
            ).expanduser()
            source_invocation_sha = atomic_evidence.get(
                "source_invocation_report_sha256"
            )
            source_cache = source.get("c1_cache_policy")
            source_cache_root = (
                source_cache.get("root") if isinstance(source_cache, dict) else None
            )
            source_run_dir = (
                Path(source_cache_root).expanduser().resolve().parent
                if isinstance(source_cache_root, str) and source_cache_root
                else None
            )
            if (
                not source_invocation.is_file()
                or source_run_dir is None
                or not source_invocation.resolve().is_relative_to(source_run_dir)
                or not isinstance(source_invocation_sha, str)
                or hashlib.sha256(source_invocation.read_bytes()).hexdigest()
                != source_invocation_sha
            ):
                raise ValueError(
                    "C1 source atomic snapshot no longer matches its invocation report"
                )
            try:
                invocation_payload = json.loads(
                    source_invocation.read_text(encoding="utf-8")
                )
            except json.JSONDecodeError as error:
                raise ValueError(
                    "C1 source atomic snapshot invocation report is unreadable"
                ) from error
            invocation_results = (
                invocation_payload.get("results")
                if isinstance(invocation_payload, dict)
                else None
            )
            matching_results = [
                value
                for value in (invocation_results or [])
                if isinstance(value, dict)
                and str(value.get("task_id") or "") == task_id
            ]
            if len(matching_results) != 1 or matching_results[0] != results[0]:
                raise ValueError(
                    "C1 source atomic snapshot result differs from its invocation report"
                )
        sidecar_path = report_path.parent / _C1_INVOCATION_CONTEXT_FILE
        if not sidecar_path.is_file():
            raise ValueError("C1 source trial is missing its invocation sidecar")
        sidecar_raw = sidecar_path.read_bytes()
        try:
            sidecar = json.loads(sidecar_raw)
        except json.JSONDecodeError as error:
            raise ValueError("C1 source trial sidecar is unreadable") from error
        expected_sidecar = {
            "schema_version": 6,
            "kind": "p5_c1_invocation_context",
            "run_id": source.get("run_id"),
            "arm": row.get("arm"),
            "model": row.get("model"),
            "seed": row.get("seed"),
            "task_id": None if shared_snapshot else task_id,
            "trial_attempt": (
                0 if shared_snapshot else row.get("trial_attempt")
            ),
            "usage_accounting": source.get("usage_accounting"),
            "c1_trial_persistence": source.get("c1_trial_persistence"),
            "c1_start_gate_protocol": source.get("c1_start_gate_protocol"),
            "c1_background_pixel_protocol": source.get(
                "c1_background_pixel_protocol"
            ),
            "c1_background_pixel_protocol_sha256": source.get(
                "c1_background_pixel_protocol_sha256"
            ),
            "c1_prompt_revisions": source.get("c1_prompt_revisions"),
            "c1_random_controls": source.get("c1_random_controls"),
            "c1_application_revisions": source.get(
                "c1_application_revisions"
            ),
            "c1_environment": source.get("c1_environment"),
            "git_commit": source.get("git_commit"),
            "manifest_hash": source.get("manifest_hash"),
            "task_manifest_hash": source.get("task_manifest_hash"),
            "harness_hash": source.get("harness_hash"),
        }
        if not isinstance(sidecar, dict) or any(
            sidecar.get(field) != value for field, value in expected_sidecar.items()
        ):
            raise ValueError("C1 source trial sidecar provenance mismatch")
        if recovery_snapshot:
            invocation = sidecar.get("invocation")
            expected_invocation_fields = {
                "c1_outer_worker": source.get("c1_outer_worker"),
                "c1_inner_worker": source.get("c1_inner_worker"),
                "task_timeout_s": source.get("c1_task_timeout_s"),
            }
            if str(row.get("arm") or "") in {
                "c1_agent_s",
                "c1_agent_s_synth",
            }:
                expected_invocation_fields["provider_timeout_s"] = source.get(
                    "provider_timeout_s"
                )
            if not isinstance(invocation, dict) or any(
                invocation.get(field) != value
                for field, value in expected_invocation_fields.items()
            ):
                raise ValueError(
                    "C1 recovery source sidecar invocation provenance mismatch"
                )
        expected_preflight = (
            {
                key: value
                for key, value in source.get("preflight", {}).items()
                if key != "artifact"
            }
            if isinstance(source.get("preflight"), dict)
            else source.get("preflight")
        )
        source_calibration = source.get("calibration")
        if isinstance(source_calibration, dict):
            calibration_fields = (
                "schema_version",
                "schema",
                "passed",
                "generated_at_utc",
                "artifact_sha256",
                "git_commit",
                "manifest_hash",
                "harness_hash",
                "c1_application_revisions",
                "c1_environment",
                "api_provider",
                "provider_timeout_s",
                "observer_model",
                "grounder_model",
                "background_pixel_protocol_sha256",
                "backends",
            )
            expected_calibration_sidecar = {
                field: source_calibration.get(field)
                for field in calibration_fields
            }
            if "provider_timeout_s" not in source_calibration:
                expected_calibration_sidecar.pop("provider_timeout_s", None)
        else:
            expected_calibration_sidecar = source_calibration
        if (
            sidecar.get("preflight") != expected_preflight
            or sidecar.get("calibration") != expected_calibration_sidecar
        ):
            raise ValueError("C1 source trial gate provenance mismatch")
        task_set = sidecar.get("task_set")
        sidecar_task_ids = task_set.get("task_ids") if isinstance(task_set, dict) else None
        if (
            not isinstance(sidecar_task_ids, list)
            or (
                task_id not in sidecar_task_ids
                if shared_snapshot
                else sidecar_task_ids != [task_id]
            )
        ):
            raise ValueError("C1 source trial sidecar does not bind its task")
        report_context = report.get("run_context")
        if not isinstance(report_context, dict) or any(
            report_context.get(field) != value
            for field, value in {
                "run_id": (
                    (
                        f"{source.get('run_id')}:{row.get('arm')}:"
                        f"{row.get('model') or 'na'}:"
                        f"{row.get('seed') if row.get('seed') is not None else 'na'}:"
                        "all:attempt0"
                    )
                    if shared_snapshot
                    else (
                        f"{source.get('run_id')}:{row.get('arm')}:"
                        f"{row.get('model') or 'na'}:"
                        f"{row.get('seed') if row.get('seed') is not None else 'na'}:"
                        f"{task_id}:attempt{row.get('trial_attempt')}"
                    )
                ),
                "arm": row.get("arm"),
                "model": row.get("model"),
                "seed": row.get("seed"),
                "task_id": None if shared_snapshot else task_id,
                "trial_attempt": (
                    0 if shared_snapshot else row.get("trial_attempt")
                ),
                "git_commit": source.get("git_commit"),
                "task_manifest_hash": source.get("task_manifest_hash"),
                "c1_start_gate_protocol": source.get(
                    "c1_start_gate_protocol"
                ),
                "c1_application_revisions": source.get(
                    "c1_application_revisions"
                ),
                "c1_environment": source.get("c1_environment"),
                "c1_prompt_revisions": source.get("c1_prompt_revisions"),
                "c1_random_controls": source.get("c1_random_controls"),
            }.items()
        ):
            raise ValueError("C1 source trial report provenance mismatch")
        if (
            report_context.get("preflight") != expected_preflight
            or report_context.get("calibration") != source.get("calibration")
        ):
            raise ValueError("C1 source trial report gate provenance mismatch")
        return (
            report_path,
            hashlib.sha256(sidecar_raw).hexdigest(),
            report_sha,
            report,
        )

    def _validate_existing_replacement_row(
        self,
        *,
        arm: str,
        model: str | None,
        seed: int | None,
        task_id: str,
        expected_replacement: dict[str, Any],
    ) -> None:
        matches = [
            row
            for row in self.index.get(arm, [])
            if row.get("model") == model
            and row.get("seed") == seed
            and row.get("task_id") == task_id
        ]
        if len(matches) != 1:
            raise ValueError("recovery root has a missing or duplicate local tuple")
        row = matches[0]
        cfg = self._arm_cfg(arm)
        task_record = next(
            (
                task
                for task in self._c1_task_records(cfg)
                if str(task.get("id") or "") == task_id
            ),
            None,
        )
        if task_record is None:
            raise ValueError("recovery task is not present in the registered C1 set")
        concurrency_invocation = {
            "c1_outer_worker": self.c1_outer_worker,
            "c1_inner_worker": self.c1_inner_worker,
            "task_timeout_s": self.c1_task_timeout_s,
            "inner_task_plan": self._c1_inner_task_plan([task_record]),
            "unity_serialized": self.c1_unity_serialized,
            "unity_serialized_benchmarks": list(
                self.c1_unity_serialized_benchmarks
            ),
            "background_browser_headless": True,
            "atomic_defer": True,
        }
        if arm in {"c1_state", "c1_state_minimal_agent"}:
            transition_source = (
                str(cfg.get("transition_action_source") or "minimal_agent_v1")
                if arm == "c1_state_minimal_agent"
                else "generated_spec"
            )
            invocation = {
                **concurrency_invocation,
                "explore": bool(cfg.get("explore", True)),
                "synthesis_ablation": str(cfg.get("synthesis_ablation", "full")),
                "max_explore_steps": int(cfg.get("max_explore_steps", 12)),
                "shared_synthesis_cache": str(
                    (self.run_dir / "c1-self-synthesis-cache").resolve()
                ),
                **(
                    {
                        "pair_start_cache": str(
                            (self.run_dir / "c1-pair-start-cache").resolve()
                        )
                    }
                    if isinstance(self.c1_start_gate_protocol, dict)
                    and isinstance(
                        self.c1_start_gate_protocol.get("pair_start_gate"), dict
                    )
                    else {}
                ),
                "max_iters": int(cfg.get("max_iters", 1)),
                "seeds": 1,
                "restore_diagnostics": bool(cfg.get("restore_diagnostics", False)),
                "restore_diagnostic_task_ids": (
                    [task_id] if bool(cfg.get("restore_diagnostics", False)) else []
                ),
                **(
                    {
                        **(
                            {
                                "transition_action_source": transition_source,
                                "source_action_cache": str(
                                    (
                                        self.run_dir / "c1-source-action-cache"
                                    ).resolve()
                                ),
                                "source_action_max_steps": int(
                                    cfg.get("source_action_max_steps", 16)
                                ),
                                "source_action_max_tokens": int(
                                    cfg.get("source_action_max_tokens", 8192)
                                ),
                                "source_action_format_repairs": int(
                                    cfg.get("source_action_format_repairs", 1)
                                ),
                                "source_action_prompt_revision": str(
                                    cfg.get("source_action_prompt_revision") or ""
                                ),
                            }
                            if transition_source == "source_agent_v2"
                            else {
                                "transition_action_source": transition_source,
                                "minimal_action_cache": str(
                                    (
                                        self.run_dir / "c1-minimal-action-cache"
                                    ).resolve()
                                ),
                                "minimal_action_max_actions": int(
                                    cfg.get("minimal_action_max_actions", 8)
                                ),
                                "minimal_action_max_tokens": int(
                                    cfg.get("minimal_action_max_tokens", 2048)
                                ),
                                "minimal_action_format_repairs": int(
                                    cfg.get("minimal_action_format_repairs", 1)
                                ),
                            }
                        ),
                    }
                    if arm == "c1_state_minimal_agent"
                    else {}
                ),
            }
        else:
            bridge = self.manifest.get("visual_bridge")
            bridge = bridge if isinstance(bridge, dict) else {}
            invocation = {
                **concurrency_invocation,
                "start_mode": str(
                    cfg.get("start_mode")
                    or ("synthesized" if arm.endswith("_synth") else "from_start")
                ),
                "policy_kind": str(
                    cfg.get("policy_kind")
                    or ("agent_s" if arm.startswith("c1_agent_s") else "gui_agent")
                ),
                "step_budgets": {
                    str(key): int(value)
                    for key, value in (cfg.get("step_budgets") or {}).items()
                },
                "observer_model": str(
                    bridge.get("observer_model") or "claude-sonnet-5@relay"
                ),
                "grounder_model": resolve_grounder_model(
                    bridge.get("grounder_model"),
                    observer_model=str(
                        bridge.get("observer_model") or "claude-sonnet-5@relay"
                    ),
                    decision_model=model,
                ),
                "provider_timeout_s": self.provider_timeout_s,
                "synth_model": model,
                "max_explore_steps": int(cfg.get("max_explore_steps", 12)),
                "shared_synthesis_cache": str(
                    (self.run_dir / "c1-self-synthesis-cache").resolve()
                ),
                "start_gate_protocol": self.c1_start_gate_protocol,
            }

        def validate_attempt_evidence(
            evidence: dict[str, Any], *, attempt: int, expected_status: str
        ) -> tuple[Path, dict[str, Any]]:
            expected_attempt_id = self._c1_attempt_id(
                arm, model, seed, task_id, attempt
            )
            if evidence.get("trial_attempt") != attempt:
                raise ValueError("existing recovery attempt ordinal mismatch")
            if evidence.get("attempt_id") != expected_attempt_id:
                raise ValueError("existing recovery attempt id mismatch")
            raw_report = evidence.get("report")
            if not isinstance(raw_report, str) or not raw_report:
                raise ValueError("existing recovery replacement report is missing")
            report_path = Path(raw_report).expanduser()
            if not report_path.is_file():
                raise ValueError("existing recovery replacement report disappeared")
            report_raw = report_path.read_bytes()
            if hashlib.sha256(report_raw).hexdigest() != evidence.get("report_sha256"):
                raise ValueError("existing recovery replacement report SHA mismatch")
            try:
                report = json.loads(report_raw)
            except json.JSONDecodeError as error:
                raise ValueError(
                    "existing recovery replacement report is unreadable"
                ) from error
            results = report.get("results") if isinstance(report, dict) else None
            contains_unknown = cost.contains_unknown_usage(report)
            expect_unknown = expected_status == "quarantined_unknown_usage"
            expect_complete = expected_status == "ok"
            if (
                not isinstance(report, dict)
                or (report.get("complete") is True) != expect_complete
                or contains_unknown != expect_unknown
                or not isinstance(results, list)
                or len(results) != 1
                or not isinstance(results[0], dict)
                or str(results[0].get("task_id") or "") != task_id
            ):
                raise ValueError(
                    "existing recovery replacement is not exact one-trial evidence"
                )
            if expected_status in {
                "exact_usage_task_timeout",
                "exact_usage_retryable_infrastructure",
            } and (
                self._registered_source_error_kind(
                    result=results[0],
                    report=report,
                    source={"c1_task_timeout_s": self.c1_task_timeout_s},
                    task_id=task_id,
                )
                != expected_status
            ):
                raise ValueError(
                    "existing recovery replacement is not registered exact-usage evidence"
                )
            sidecar_path = report_path.parent / _C1_INVOCATION_CONTEXT_FILE
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    "existing recovery replacement sidecar is unreadable"
                ) from error
            task_set = sidecar.get("task_set") if isinstance(sidecar, dict) else None
            task_file = task_set.get("task_file") if isinstance(task_set, dict) else None
            if not isinstance(task_file, str) or not task_file:
                raise ValueError(
                    "existing recovery replacement sidecar task file is missing"
                )
            expected_sidecar = self._c1_invocation_context(
                arm,
                model=model,
                seed=seed,
                tasks_path=task_file,
                invocation=invocation,
                task_id=task_id,
                attempt=attempt,
            )
            if not isinstance(sidecar, dict) or sidecar != expected_sidecar:
                raise ValueError(
                    "existing recovery replacement sidecar provenance mismatch"
                )
            report_context = report.get("run_context")
            critical_report_context = {
                "run_id": expected_attempt_id,
                "arm": arm,
                "model": model,
                "seed": seed,
                "task_id": task_id,
                "trial_attempt": attempt,
                "git_commit": self.git_commit,
                "task_manifest_hash": self.task_manifest_hash,
                "runtime_lock": self.runtime_lock_policy,
                "preflight": self._preflight_provenance(),
                "calibration": self.calibration_metadata,
                "usage_accounting": self.usage_accounting,
                "c1_trial_persistence": self.c1_trial_persistence,
                "c1_start_gate_protocol": self.c1_start_gate_protocol,
            }
            if not isinstance(report_context, dict) or any(
                report_context.get(field) != value
                for field, value in critical_report_context.items()
            ):
                raise ValueError(
                    "existing recovery replacement report provenance mismatch"
                )
            return report_path, report

        local_overhead = [
            item
            for item in self.c1_operational_overhead
            if item.get("arm") == arm
            and item.get("model") == model
            and item.get("seed") == seed
            and item.get("task_id") == task_id
            and item.get("source_provenance") is None
        ]
        status = row.get("status")
        if status == "pending":
            if any(
                row.get(field) is not None
                for field in (
                    "trial_attempt",
                    "attempt_id",
                    "report",
                    "report_sha256",
                    "replacement_for",
                    "source_provenance",
                )
            ) or row.get("imported") is True:
                raise ValueError("pending recovery tuple has fabricated provenance")
            if local_overhead:
                raise ValueError("pending recovery tuple has unknown-attempt overhead")
            return
        if (
            status not in {"ok", "quarantined_unknown_usage", "error"}
            or row.get("replacement_for") != expected_replacement
            or row.get("source_provenance") is not None
            or row.get("imported") is True
        ):
            raise ValueError("existing recovery tuple has invalid replacement provenance")
        attempt = row.get("trial_attempt")
        maximum = int(self.c1_trial_persistence["max_recovery_attempts_per_tuple"])
        if (
            not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 0
            or attempt >= maximum
        ):
            raise ValueError("existing recovery replacement attempt is out of bounds")
        current_overhead = next(
            (
                item
                for item in local_overhead
                if item.get("trial_attempt") == attempt
            ),
            None,
        )
        validate_attempt_evidence(
            row,
            attempt=attempt,
            expected_status=(
                "ok"
                if status == "ok"
                else (
                    "quarantined_unknown_usage"
                    if status == "quarantined_unknown_usage"
                    else str(current_overhead.get("status") or "error")
                    if isinstance(current_overhead, dict)
                    else "error"
                )
            ),
        )
        # A nonretryable incomplete producer report is the terminal row itself,
        # not retry overhead.  Earlier retryable attempts remain contiguous in
        # operational overhead; include the current ordinal only when it was
        # actually registered there.
        expected_noncompleted_attempts = set(
            range(attempt + (status != "ok" and current_overhead is not None))
        )
        overhead_by_attempt: dict[int, dict[str, Any]] = {}
        for overhead in local_overhead:
            ordinal = overhead.get("trial_attempt")
            if (
                not isinstance(ordinal, int)
                or isinstance(ordinal, bool)
                or ordinal in overhead_by_attempt
            ):
                raise ValueError("recovery noncompleted-attempt ledger is malformed")
            overhead_by_attempt[ordinal] = overhead
        if set(overhead_by_attempt) != expected_noncompleted_attempts:
            raise ValueError("recovery noncompleted-attempt ledger is not contiguous")
        for ordinal, overhead in overhead_by_attempt.items():
            if (
                overhead.get("status")
                not in {
                    "quarantined_unknown_usage",
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                }
                or overhead.get("replacement_for") != expected_replacement
            ):
                raise ValueError("recovery noncompleted-attempt provenance mismatch")
            report_path, _report = validate_attempt_evidence(
                overhead,
                attempt=ordinal,
                expected_status=str(overhead.get("status")),
            )
            if overhead.get("known_and_unknown_usage") != self._unknown_usage_snapshot(
                str(report_path)
            ):
                raise ValueError("recovery noncompleted-attempt usage snapshot mismatch")
        if status == "quarantined_unknown_usage" and current_overhead is None:
            raise ValueError("latest recovery quarantine row is missing from its ledger")
        if current_overhead is not None:
            latest = overhead_by_attempt[attempt]
            if any(
                latest.get(field) != row.get(field)
                for field in (
                    "attempt_id",
                    "trial_attempt",
                    "report",
                    "report_sha256",
                    "error",
                    "replacement_for",
                )
            ):
                raise ValueError("latest recovery quarantine row differs from its ledger")

    def _import_c1_source_trials(self) -> None:
        """Import every completed tuple from one compatible trial-atomic run.

        The import is deliberately all-or-nothing.  A source must enumerate the
        entire requested matrix, including quarantined tuples, so deleting an
        inconvenient completed outcome cannot turn it into a replacement.
        """

        if self._c1_source_imported or self.c1_source_index is None:
            return
        source_path = self.c1_source_index
        if source_path.resolve() == self._index_path.resolve():
            raise ValueError("C1 source index must come from a different clean root")
        try:
            source_raw = source_path.read_bytes()
            source = json.loads(source_raw)
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("C1 source index is unreadable") from error
        if not isinstance(source, dict):
            raise ValueError("C1 source index must be an object")
        source_sha = hashlib.sha256(source_raw).hexdigest()
        direct_upstream_sources = copy.deepcopy(source.get("c1_sources"))
        if direct_upstream_sources:
            try:
                migration_preview = json.loads(
                    self.c1_source_contract_migration.read_text(encoding="utf-8")
                ) if self.c1_source_contract_migration is not None else None
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(
                    "C1 chained source requires a readable registered migration"
                ) from error
            if (
                not isinstance(migration_preview, dict)
                or migration_preview.get("migration_id")
                not in {
                    _BACKGROUND_RESOURCE_MIGRATION_ID,
                    "c1_unity_license_serialization_v1",
                    "c1_atomic_defer_sidecar_cleanup_v1",
                    "c1_source_action_sidecar_validation_v1",
                }
            ):
                raise ValueError(
                    "C1 source chaining is not registered for this migration"
                )
            source = _flatten_persisted_c1_source_for_lineage(
                source_path=source_path,
                source=source,
                source_sha=source_sha,
            )
        current_harness = self._freeze_c1_harness_hash()
        migration_needed: list[str] = []
        if source.get("c1_trial_persistence") != self.c1_trial_persistence:
            raise ValueError(
                "C1 source index is legacy or uses a different trial schema"
            )
        if source.get("usage_accounting") != self.usage_accounting:
            source_usage = copy.deepcopy(source.get("usage_accounting"))
            removed = None
            if isinstance(source_usage, dict):
                detail = source_usage.get("reasoning_detail")
                if isinstance(detail, dict):
                    removed = detail.pop("above_threshold", None)
            if removed != "paper_ready_false" or source_usage != self.usage_accounting:
                raise ValueError(
                    "C1 source index uses a different usage accounting schema"
                )
            migration_needed.append("usage_accounting_remove_paper_ready")
        source_cache_policy = source.get("c1_cache_policy")
        source_cache_root = (
            source_cache_policy.get("root")
            if isinstance(source_cache_policy, dict)
            else None
        )
        if not isinstance(source_cache_root, str) or not source_cache_root:
            raise ValueError("C1 source index is missing its run-scoped cache root")
        source_run_dir = Path(source_cache_root).expanduser().resolve().parent
        flattened_lineage = source.get("flattened_source_lineage")
        source_identity_path = source_path
        if isinstance(flattened_lineage, dict):
            derived_from_index = flattened_lineage.get("derived_from_index")
            if not isinstance(derived_from_index, str) or not derived_from_index:
                raise ValueError("C1 flattened recovery derivation path is missing")
            source_identity_path = Path(derived_from_index).expanduser().resolve()
        if (
            source.get("run_id") == self.run_id
            or source_run_dir == self.run_dir.resolve()
            or source_identity_path.parent != source_run_dir
        ):
            raise ValueError(
                "C1 source index must be the original index from a distinct clean root"
            )
        compatibility = {
            "manifest_hash": self.manifest_hash,
            "task_manifest_hash": self.task_manifest_hash,
            "harness_hash": current_harness,
            "git_commit": self.git_commit,
            "models": self.models,
            "seeds": self.seeds,
            "task_ids": self.task_ids,
            "arms_run": self.arms,
            "agent_kind": self.agent_kind,
            "headless": self.headless,
            "model_request_profiles": self.model_request_profiles,
            "model_request_profiles_sha256": self.model_request_profiles_sha256,
            "runtime_lock": self.runtime_lock_policy,
            "provider_timeout_s": self.provider_timeout_s,
            "c1_task_timeout_s": self.c1_task_timeout_s,
            "c1_start_gate_protocol": self.c1_start_gate_protocol,
            "c1_background_pixel_protocol": self.c1_background_pixel_protocol,
            "c1_background_pixel_protocol_sha256": (
                self.c1_background_pixel_protocol_sha256
            ),
            "c1_application_revisions": self.c1_application_revisions,
            "c1_environment": self.c1_environment,
            "c1_prompt_revisions": self._c1_prompt_revision_provenance(),
            "c1_random_controls": self._c1_random_control_provenance(),
        }
        mismatches = [
            field for field, value in compatibility.items() if source.get(field) != value
        ]
        application_revisions_rebind = set(mismatches) == {
            "git_commit",
            "harness_hash",
            "task_manifest_hash",
            "c1_application_revisions",
        }
        harness_rebind = set(mismatches) in (
            {"harness_hash"},
            {"git_commit", "harness_hash"},
        ) or application_revisions_rebind
        if mismatches and not (
            harness_rebind and self.c1_source_contract_migration is not None
        ):
            raise ValueError(
                "C1 source index is incompatible: " + ", ".join(mismatches)
            )
        if harness_rebind:
            migration_needed.extend(["git_commit_rebind", "harness_rebind"])
        if application_revisions_rebind:
            migration_needed.extend(
                ["c1_application_revisions_rebind", "task_manifest_hash_rebind"]
            )

        def without_migration_fields(
            value: dict[str, Any] | None,
        ) -> dict[str, Any] | None:
            if value is None:
                return None
            if not harness_rebind:
                return value
            migration_fields = {"git_commit", "manifest_hash", "harness_hash"}
            if application_revisions_rebind:
                migration_fields.add("c1_application_revisions")
            return {
                key: item
                for key, item in value.items()
                if key not in migration_fields
            }

        source_preflight = self._preflight_scientific_contract(source.get("preflight"))
        current_preflight = self._preflight_scientific_contract(
            self.preflight_metadata
        )
        if source_preflight != current_preflight:
            if without_migration_fields(source_preflight) != without_migration_fields(
                current_preflight
            ):
                raise ValueError(
                    "C1 source preflight scientific contract differs from the clean root"
                )
            migration_needed.append("preflight_rebind")
        source_calibration = self._calibration_scientific_contract(
            source.get("calibration")
        )
        current_calibration = self._calibration_scientific_contract(
            self.calibration_metadata
        )
        if source_calibration != current_calibration:
            if without_migration_fields(
                source_calibration
            ) != without_migration_fields(current_calibration):
                raise ValueError(
                    "C1 source calibration scientific contract differs from the clean root"
                )
            migration_needed.append("calibration_rebind")

        migration_needed = sorted(set(migration_needed))
        if migration_needed:
            if self.c1_source_contract_migration is None:
                raise ValueError(
                    "C1 source contract migration is required for: "
                    + ", ".join(migration_needed)
                )
            try:
                migration_raw = self.c1_source_contract_migration.read_bytes()
                migration = json.loads(migration_raw)
            except (OSError, json.JSONDecodeError) as error:
                raise ValueError(
                    "C1 source contract migration artifact is unreadable"
                ) from error
            expected_migration = {
                "schema_version": 2 if harness_rebind else 1,
                "kind": "c1_source_contract_migration",
                "source_index": str(source_path),
                "source_index_sha256": source_sha,
                "source_run_id": source.get("run_id"),
                "source_manifest_hash": source.get("manifest_hash"),
                "source_harness_hash": source.get("harness_hash"),
                "current_manifest_hash": self.manifest_hash,
                "current_harness_hash": current_harness,
                "allowed_migrations": migration_needed,
                "replaceable_source_statuses": [
                    "pending",
                    "quarantined_unknown_usage",
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                    *(
                        ["registered_exact_usage_harness_failure"]
                        if harness_rebind
                        else []
                    ),
                ],
            }
            migration_mismatches = (
                list(expected_migration)
                if not isinstance(migration, dict)
                else [
                    field
                    for field, value in expected_migration.items()
                    if migration.get(field) != value
                ]
            )
            if migration_mismatches:
                raise ValueError(
                    "C1 source contract migration artifact does not bind the exact "
                    "migration: "
                    + ", ".join(migration_mismatches)
                )
            if not isinstance(migration.get("reason"), str) or not migration[
                "reason"
            ].strip():
                raise ValueError("C1 source contract migration reason is missing")
            if harness_rebind:
                _validate_registered_c1_harness_migration(
                    base=self.base,
                    source=source,
                    migration=migration,
                )
            self.c1_source_contract_migration_metadata = {
                **copy.deepcopy(migration),
                "artifact": str(self.c1_source_contract_migration),
                "artifact_sha256": hashlib.sha256(migration_raw).hexdigest(),
            }
        elif self.c1_source_contract_migration is not None:
            raise ValueError("C1 source contract migration was supplied but is unnecessary")

        interrupted_attempts_by_key: dict[TrialKey, dict[str, Any]] = {}
        pause_audit_path: Path | None = None
        pause_audit_sha: str | None = None
        migration_metadata = self.c1_source_contract_migration_metadata or {}
        if (
            migration_metadata.get("migration_id")
            == _BACKGROUND_RESOURCE_MIGRATION_ID
            and migration_metadata.get("pause_audit")
        ):
            pause_audit_path = Path(
                str(migration_metadata.get("pause_audit") or "")
            ).expanduser()
            pause_raw = pause_audit_path.read_bytes()
            pause_audit_sha = hashlib.sha256(pause_raw).hexdigest()
            if pause_audit_sha != migration_metadata.get("pause_audit_sha256"):
                raise ValueError("C1 pause audit changed after migration validation")
            pause_payload = json.loads(pause_raw)
            interrupted_rows = pause_payload.get(
                "interrupted_unknown_usage_attempts"
            )
            if interrupted_rows != migration_metadata.get(
                "interrupted_unknown_usage_attempts"
            ):
                raise ValueError("C1 interrupted-attempt migration ledger changed")
            for item in interrupted_rows:
                key = self._trial_key(
                    str(item.get("arm") or ""),
                    item.get("model"),
                    item.get("seed"),
                    str(item.get("task_id") or ""),
                )
                if key in interrupted_attempts_by_key:
                    raise ValueError("duplicate C1 interrupted-attempt migration tuple")
                interrupted_attempts_by_key[key] = copy.deepcopy(item)

        flattened_source = isinstance(flattened_lineage, dict)
        upstream_sources: dict[str, tuple[Path, dict[str, Any], str]] = {}

        def load_flattened_upstream(
            record: dict[str, Any],
        ) -> tuple[Path, dict[str, Any], str]:
            if not isinstance(record, dict):
                raise ValueError("C1 flattened upstream provenance is malformed")
            upstream_path = Path(str(record.get("source_index") or "")).expanduser()
            cache_key = str(upstream_path)
            cached = upstream_sources.get(cache_key)
            if cached is None:
                if not upstream_path.is_file():
                    raise ValueError("C1 flattened upstream source is missing")
                upstream_raw = upstream_path.read_bytes()
                upstream_sha = hashlib.sha256(upstream_raw).hexdigest()
                try:
                    upstream_payload = json.loads(upstream_raw)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "C1 flattened upstream source is unreadable"
                    ) from error
                if not isinstance(upstream_payload, dict):
                    raise ValueError(
                        "C1 flattened upstream source provenance mismatch"
                    )
                upstream_payload = _flatten_persisted_c1_source_for_lineage(
                    source_path=upstream_path,
                    source=upstream_payload,
                    source_sha=upstream_sha,
                )
                cached = (upstream_path, upstream_payload, upstream_sha)
                upstream_sources[cache_key] = cached
            if (
                cached[2] != record.get("source_index_sha256")
                or record.get("source_run_id") != cached[1].get("run_id")
            ):
                raise ValueError("C1 flattened upstream source SHA mismatch")
            return cached

        if flattened_source:
            records = flattened_lineage.get("upstream_sources")
            if not isinstance(records, list) or len(records) != 1:
                raise ValueError(
                    "C1 flattened recovery source must bind one upstream source"
                )
            for record in records:
                load_flattened_upstream(record)

        first_cfg = self._arm_cfg(self.arms[0])
        task_ids = [str(task["id"]) for task in self._c1_task_records(first_cfg)]
        expected_keys = {
            self._trial_key(arm, model, seed, task_id)
            for arm in self.arms
            for model in self.models
            for seed in range(self.seeds)
            for task_id in task_ids
        }
        source_rows: dict[TrialKey, dict[str, Any]] = {}
        for arm in self.arms:
            for raw_row in source.get("arms", {}).get(arm, []) or []:
                if not isinstance(raw_row, dict):
                    raise ValueError("C1 source index contains a malformed trial row")
                row = dict(raw_row)
                row["arm"] = arm
                task_id = str(row.get("task_id") or "")
                key = self._trial_key(arm, row.get("model"), row.get("seed"), task_id)
                if key in source_rows:
                    raise ValueError("C1 source index contains a duplicate trial tuple")
                source_rows[key] = row
        if set(source_rows) != expected_keys:
            missing = sorted(expected_keys - set(source_rows), key=str)
            extra = sorted(set(source_rows) - expected_keys, key=str)
            raise ValueError(
                "C1 source index must enumerate the complete tuple matrix; "
                f"missing={len(missing)} extra={len(extra)}"
            )
        overhead_by_key: dict[TrialKey, list[dict[str, Any]]] = {}
        for raw in source.get("c1_operational_overhead") or []:
            if not isinstance(raw, dict):
                raise ValueError("C1 source operational overhead is malformed")
            key = self._trial_key(
                str(raw.get("arm") or ""),
                raw.get("model"),
                raw.get("seed"),
                str(raw.get("task_id") or ""),
            )
            overhead_by_key.setdefault(key, []).append(dict(raw))

        if self.c1_sources:
            if len(self.c1_sources) != 1 or any(
                row.get("source_index") != str(source_path)
                or row.get("source_index_sha256") != source_sha
                for row in self.c1_sources
            ):
                raise ValueError(
                    "resumed C1 recovery source differs from its stored provenance"
                )
            self.c1_sources.clear()
            self.c1_operational_overhead = [
                row
                for row in self.c1_operational_overhead
                if not (
                    isinstance(row.get("source_provenance"), dict)
                    and row["source_provenance"].get("source_index_sha256")
                    == source_sha
                )
            ]
        completed_rows: list[tuple[str, dict[str, Any]]] = []
        preserved_model_failure_keys = {
            self._trial_key(
                str(item["arm"]),
                item["model"],
                item["seed"],
                str(item["task_id"]),
            )
            for item in (
                (self.c1_source_contract_migration_metadata or {}).get(
                    "preserved_model_failure_trials"
                )
                or []
            )
            if isinstance(item, dict)
        }
        migration_metadata = self.c1_source_contract_migration_metadata or {}
        preserved_operational_failure_keys = {
            self._trial_key(
                str(item["arm"]), item["model"], item["seed"], str(item["task_id"])
            )
            for item in migration_metadata.get("preserved_operational_failure_trials", [])
            if isinstance(item, dict)
        }
        imported_operational_failure_keys: set[TrialKey] = set()
        harness_failure_trials = migration_metadata.get(
            "registered_harness_failure_trials",
            migration_metadata.get("affected_trials") or [],
        )
        registered_harness_failure_keys = {
            self._trial_key(
                str(item["arm"]),
                item["model"],
                item["seed"],
                str(item["task_id"]),
            )
            for item in harness_failure_trials
            if isinstance(item, dict)
        }
        imported_model_failure_keys: set[TrialKey] = set()
        inherited_model_failure_keys: set[TrialKey] = set()
        quarantine_keys: set[TrialKey] = set()
        exact_timeout_keys: set[TrialKey] = set()
        retryable_infrastructure_keys: set[TrialKey] = set()
        replacement_keys: set[TrialKey] = set()

        def source_row_for_key(
            payload: dict[str, Any],
            arm: str,
            key: TrialKey,
        ) -> dict[str, Any]:
            matches = [
                item
                for item in (payload.get("arms", {}).get(arm) or [])
                if isinstance(item, dict)
                and self._trial_key(
                    arm,
                    item.get("model"),
                    item.get("seed"),
                    str(item.get("task_id") or ""),
                )
                == key
            ]
            if len(matches) != 1:
                raise ValueError(
                    "C1 flattened upstream source does not bind one trial tuple"
                )
            return matches[0]

        def resolve_flattened_evidence(
            *,
            payload_path: Path,
            payload: dict[str, Any],
            payload_sha: str,
            row: dict[str, Any],
            arm: str,
            key: TrialKey,
            visited: set[tuple[str, str]],
        ) -> tuple[Path, dict[str, Any], str, str]:
            visit_key = (str(payload_path), payload_sha)
            if visit_key in visited:
                raise ValueError("C1 flattened source lineage contains a cycle")
            visited = {*visited, visit_key}
            if row.get("imported") is not True:
                if row.get("source_provenance") is not None:
                    raise ValueError(
                        "C1 flattened local tuple has fabricated source provenance"
                    )
                return payload_path, payload, payload_sha, str(row.get("status"))

            lineage = payload.get("flattened_source_lineage")
            records = lineage.get("upstream_sources") if isinstance(lineage, dict) else None
            provenance = row.get("source_provenance")
            if (
                not isinstance(records, list)
                or len(records) != 1
                or not isinstance(provenance, dict)
            ):
                raise ValueError("C1 flattened imported tuple provenance is malformed")
            upstream = load_flattened_upstream(records[0])
            upstream_path, upstream_payload, upstream_sha = upstream
            if (
                str(Path(str(provenance.get("source_index") or "")).expanduser())
                != str(upstream_path)
                or provenance.get("source_index_sha256") != upstream_sha
                or provenance.get("source_run_id") != upstream_payload.get("run_id")
            ):
                raise ValueError("C1 flattened imported tuple origin SHA mismatch")
            upstream_row = source_row_for_key(upstream_payload, arm, key)
            immutable_fields = (
                "model",
                "seed",
                "task_id",
                "trial_attempt",
                "attempt_id",
                "report",
                "report_sha256",
                "replacement_for",
            )
            if any(row.get(field) != upstream_row.get(field) for field in immutable_fields):
                raise ValueError(
                    "C1 flattened imported tuple differs from its upstream evidence"
                )
            (
                evidence_path,
                evidence_payload,
                evidence_sha,
                evidence_status,
            ) = resolve_flattened_evidence(
                payload_path=upstream_path,
                payload=upstream_payload,
                payload_sha=upstream_sha,
                row=upstream_row,
                arm=arm,
                key=key,
                visited=visited,
            )
            flattened_provenance = provenance.get("flattened_source_lineage")
            expected_flattened_provenance = {
                "evidence_source_index": str(evidence_path),
                "evidence_source_index_sha256": evidence_sha,
                "evidence_source_run_id": evidence_payload.get("run_id"),
                "prior_source_provenance": copy.deepcopy(
                    upstream_row.get("source_provenance")
                ),
            }
            direct_original_source = (
                flattened_provenance is None
                and upstream_row.get("imported") is not True
                and evidence_path == upstream_path
                and evidence_sha == upstream_sha
            )
            if (
                not direct_original_source
                and flattened_provenance != expected_flattened_provenance
            ):
                raise ValueError(
                    "C1 flattened imported tuple recursive lineage mismatch"
                )
            return (
                evidence_path,
                evidence_payload,
                evidence_sha,
                str(provenance.get("source_status") or evidence_status),
            )

        for key in sorted(expected_keys, key=str):
            arm, model, seed, task_id = key
            row = source_rows[key]
            status = row.get("status")
            interrupted_attempt = interrupted_attempts_by_key.get(key)
            if status not in {"ok", "pending", "quarantined_unknown_usage", "error"}:
                raise ValueError(
                    "C1 source contains a non-replaceable incomplete tuple: "
                    f"{arm}/{model}/seed{seed}/{task_id} status={status}"
                )
            evidence_source = source
            evidence_source_path = source_path
            evidence_status = str(status)
            prior_source_provenance = row.get("source_provenance")
            if flattened_source:
                if row.get("imported") is True:
                    (
                        evidence_source_path,
                        evidence_source,
                        evidence_source_sha,
                        evidence_status,
                    ) = resolve_flattened_evidence(
                        payload_path=source_path,
                        payload=source,
                        payload_sha=source_sha,
                        row=row,
                        arm=arm,
                        key=key,
                        visited=set(),
                    )
                elif prior_source_provenance is not None:
                    raise ValueError(
                        "C1 flattened local tuple has fabricated source provenance"
                    )
                elif status == "pending":
                    if row.get("replacement_for") is not None:
                        raise ValueError(
                            "C1 flattened pending tuple has replacement evidence"
                        )
                else:
                    replacement = row.get("replacement_for")
                    if not isinstance(replacement, dict):
                        raise ValueError(
                            "C1 flattened recovery tuple lacks replacement provenance"
                        )
                    replacement_path = str(
                        Path(str(replacement.get("source_index") or "")).expanduser()
                    )
                    upstream_match = upstream_sources.get(replacement_path)
                    if (
                        upstream_match is None
                        or replacement.get("source_index_sha256")
                        != upstream_match[2]
                        or replacement.get("source_run_id")
                        != upstream_match[1].get("run_id")
                    ):
                        raise ValueError(
                            "C1 flattened recovery tuple replacement provenance mismatch"
                        )
            elif (
                row.get("imported") is True
                or row.get("replacement_for") is not None
                or prior_source_provenance is not None
            ):
                raise ValueError("C1 source trial has non-original provenance")
            inherited_completion_class = (
                prior_source_provenance.get("completion_class")
                if isinstance(prior_source_provenance, dict)
                else None
            )
            inherited_preserved_model_failure = bool(
                status == "ok"
                and evidence_status == "error"
                and isinstance(prior_source_provenance, dict)
                and prior_source_provenance.get("source_status") == "error"
                and isinstance(inherited_completion_class, str)
                and inherited_completion_class
            )
            if inherited_preserved_model_failure:
                inherited_model_failure_keys.add(key)
            if status == "pending":
                if any(
                    row.get(field) is not None
                    for field in (
                        "trial_attempt",
                        "attempt_id",
                        "report",
                        "report_sha256",
                    )
                ):
                    raise ValueError("C1 source pending trial has fabricated evidence")
                if interrupted_attempt is not None and (
                    interrupted_attempt.get("trial_attempt") != 0
                    or interrupted_attempt.get("usage_status") != "unknown"
                ):
                    raise ValueError(
                        "registered interrupted C1 pending attempt is malformed"
                    )
            elif interrupted_attempt is not None:
                raise ValueError(
                    "registered interrupted C1 tuple is not pending in the source"
                )
            else:
                source_attempt = row.get("trial_attempt")
                maximum_attempts = int(
                    self.c1_trial_persistence["max_recovery_attempts_per_tuple"]
                )
                expected_source_attempt_id = (
                    f"{evidence_source.get('run_id')}:{arm}:{model or 'na'}:"
                    f"{seed if seed is not None else 'na'}:{task_id}:"
                    f"attempt{source_attempt}"
                )
                if (
                    not isinstance(source_attempt, int)
                    or isinstance(source_attempt, bool)
                    or source_attempt < 0
                    or source_attempt >= maximum_attempts
                    or row.get("attempt_id") != expected_source_attempt_id
                ):
                    raise ValueError("C1 source trial attempt provenance mismatch")
            report_path: Path | None = None
            sidecar_sha: str | None = None
            source_report_sha: str | None = None
            source_report: dict[str, Any] | None = None
            if status != "pending":
                (
                    report_path,
                    sidecar_sha,
                    source_report_sha,
                    source_report,
                ) = self._validate_source_trial_artifacts(
                    row=row,
                    source=evidence_source,
                    source_index_path=evidence_source_path,
                    source_status=evidence_status,
                    allow_nonretryable_model_failure=(
                        key in preserved_model_failure_keys
                        or inherited_preserved_model_failure
                    ),
                    allow_registered_harness_failure=key
                    in registered_harness_failure_keys,
                )
            source_provenance = {
                "source_index": str(source_path),
                "source_index_sha256": source_sha,
                "source_run_id": source.get("run_id"),
                "source_cache_policy": source.get("c1_cache_policy"),
                "source_preflight": source.get("preflight"),
                "source_calibration": source.get("calibration"),
                "source_sidecar_sha256": sidecar_sha,
            }
            if flattened_source:
                source_provenance["flattened_source_lineage"] = {
                    "evidence_source_index": str(evidence_source_path),
                    "evidence_source_index_sha256": (
                        hashlib.sha256(evidence_source_path.read_bytes()).hexdigest()
                        if evidence_source_path.is_file()
                        else None
                    ),
                    "evidence_source_run_id": evidence_source.get("run_id"),
                    "prior_source_provenance": copy.deepcopy(
                        prior_source_provenance
                    ),
                }
            if status == "ok":
                assert report_path is not None
                imported = copy.deepcopy(row)
                imported.pop("arm", None)
                imported["report"] = str(report_path)
                imported["imported"] = True
                if key in preserved_model_failure_keys or inherited_preserved_model_failure:
                    assert source_report is not None
                    preserved_kind = self._registered_source_model_failure_kind(
                        result=source_report["results"][0],
                        report=source_report,
                    )
                    if (
                        evidence_status != "error"
                        or preserved_kind is None
                        or (
                            inherited_preserved_model_failure
                            and preserved_kind != inherited_completion_class
                        )
                    ):
                        raise ValueError(
                            "flattened preserved C1 model failure no longer matches "
                            "its exact-usage evidence"
                        )
                    source_provenance["source_status"] = "error"
                    source_provenance["completion_class"] = preserved_kind
                    imported_model_failure_keys.add(key)
                imported["source_provenance"] = source_provenance
                completed_rows.append((arm, imported))
            elif key in preserved_model_failure_keys:
                assert report_path is not None
                assert source_report is not None
                preserved_kind = self._registered_source_model_failure_kind(
                    result=source_report["results"][0],
                    report=source_report,
                )
                if preserved_kind is None:
                    raise ValueError(
                        "registered preserved C1 model failure no longer matches "
                        "its exact-usage evidence"
                    )
                imported = copy.deepcopy(row)
                imported.pop("arm", None)
                imported["status"] = "ok"
                imported["error"] = None
                imported["report"] = str(report_path)
                imported["imported"] = True
                imported["source_provenance"] = {
                    **source_provenance,
                    "source_status": "error",
                    "completion_class": preserved_kind,
                }
                completed_rows.append((arm, imported))
                imported_model_failure_keys.add(key)
            elif key in preserved_operational_failure_keys:
                assert report_path is not None and source_report is not None
                source_kind = self._registered_source_error_kind(
                    result=source_report["results"][0], report=source_report,
                    source=source, task_id=task_id,
                )
                if source_kind not in {
                    "exact_usage_task_timeout",
                    "exact_usage_retryable_infrastructure",
                }:
                    raise ValueError(
                        "registered preserved C1 operational failure is not exact usage"
                    )
                imported = copy.deepcopy(row)
                imported.pop("arm", None)
                imported["report"] = str(report_path)
                imported["imported"] = True
                imported["source_provenance"] = {
                    **source_provenance,
                    "source_status": "error",
                    "completion_class": "exact_usage_exhausted_operational_failure",
                    "source_error_kind": source_kind,
                }
                completed_rows.append((arm, imported))
                imported_operational_failure_keys.add(key)
            else:
                replacement_keys.add(key)
                replacement_source_status = (
                    "quarantined_unknown_usage"
                    if interrupted_attempt is not None
                    else status
                )
                expected_replacement = {
                    "source_index": str(source_path),
                    "source_index_sha256": source_sha,
                    "source_run_id": source.get("run_id"),
                    "source_status": replacement_source_status,
                    "source_report": (
                        str(pause_audit_path)
                        if interrupted_attempt is not None
                        else (str(report_path) if report_path else None)
                    ),
                    "source_report_sha256": (
                        pause_audit_sha
                        if interrupted_attempt is not None
                        else source_report_sha
                    ),
                }
                self._c1_replacement_sources[key] = expected_replacement
                self._validate_existing_replacement_row(
                    arm=arm,
                    model=model,
                    seed=seed,
                    task_id=task_id,
                    expected_replacement=expected_replacement,
                )
                if interrupted_attempt is not None:
                    assert pause_audit_path is not None
                    assert pause_audit_sha is not None
                    quarantine_keys.add(key)
                    self.c1_operational_overhead.append(
                        {
                            "attempt_id": (
                                f"{source.get('run_id')}:{arm}:{model or 'na'}:"
                                f"{seed if seed is not None else 'na'}:{task_id}:"
                                f"attempt{interrupted_attempt['trial_attempt']}"
                            ),
                            "arm": arm,
                            "model": model,
                            "seed": seed,
                            "task_id": task_id,
                            "trial_attempt": interrupted_attempt["trial_attempt"],
                            "status": "quarantined_unknown_usage",
                            "report": str(pause_audit_path),
                            "report_sha256": pause_audit_sha,
                            "known_and_unknown_usage": (
                                _interrupted_unknown_usage_snapshot()
                            ),
                            "error": (
                                "externally interrupted before the in-memory "
                                "provider usage ledger was persisted"
                            ),
                            "replacement_for": expected_replacement,
                            "source_provenance": {
                                **source_provenance,
                                "interrupted_attempt": copy.deepcopy(
                                    interrupted_attempt
                                ),
                            },
                        }
                    )
            if status == "quarantined_unknown_usage":
                assert report_path is not None
                quarantine_keys.add(key)
                overhead_candidates = [
                    overhead
                    for overhead in overhead_by_key.get(key, [])
                    if overhead.get("source_provenance") is None
                    and all(
                        overhead.get(field) == row.get(field)
                        for field in (
                            "attempt_id",
                            "trial_attempt",
                            "status",
                            "report",
                            "report_sha256",
                            "error",
                            "replacement_for",
                        )
                    )
                ]
                if len(overhead_candidates) != 1:
                    raise ValueError(
                        "quarantined C1 source tuple does not bind exactly one "
                        "operational-overhead record"
                    )
                overhead = overhead_candidates[0]
                if overhead.get(
                    "known_and_unknown_usage"
                ) != self._unknown_usage_snapshot(str(report_path)):
                    raise ValueError(
                        "quarantined C1 source tuple usage snapshot mismatch"
                    )
                imported_overhead = copy.deepcopy(overhead)
                imported_overhead["source_provenance"] = source_provenance
                self.c1_operational_overhead.append(imported_overhead)
            elif status == "error" and key not in (
                preserved_model_failure_keys | preserved_operational_failure_keys
            ):
                assert report_path is not None
                assert source_report_sha is not None
                assert source_report is not None
                source_error_result = source_report["results"][0]
                source_error_kind = self._registered_source_error_kind(
                    result=source_error_result,
                    report=source_report,
                    source=source,
                    task_id=task_id,
                )
                if (
                    source_error_kind is None
                    and key in registered_harness_failure_keys
                ):
                    source_error_kind = "registered_exact_usage_harness_failure"
                assert source_error_kind is not None
                if source_error_kind == "exact_usage_task_timeout":
                    exact_timeout_keys.add(key)
                else:
                    retryable_infrastructure_keys.add(key)
                self.c1_operational_overhead.append(
                    {
                        "attempt_id": row.get("attempt_id"),
                        "arm": arm,
                        "model": model,
                        "seed": seed,
                        "task_id": task_id,
                        "trial_attempt": row.get("trial_attempt"),
                        "status": f"replaced_{source_error_kind}",
                        "report": str(report_path),
                        "report_sha256": source_report_sha,
                        "known_and_unknown_usage": copy.deepcopy(
                            source_error_result.get("actual_cost")
                            or source_error_result.get("cost")
                        ),
                        "error": source_error_result.get("reason"),
                        "replacement_for": self._c1_replacement_sources[key],
                        "source_provenance": source_provenance,
                    }
                )
        if flattened_source:
            for key, overhead_rows in overhead_by_key.items():
                for overhead in overhead_rows:
                    prior_provenance = overhead.get("source_provenance")
                    if isinstance(prior_provenance, dict):
                        raw_report = overhead.get("report")
                        report_path = (
                            Path(str(raw_report)).expanduser()
                            if isinstance(raw_report, str) and raw_report
                            else None
                        )
                        if (
                            report_path is None
                            or not report_path.is_file()
                            or hashlib.sha256(report_path.read_bytes()).hexdigest()
                            != overhead.get("report_sha256")
                        ):
                            raise ValueError(
                                "C1 flattened operational-overhead evidence mismatch"
                            )
                        imported_overhead = copy.deepcopy(overhead)
                        imported_overhead["source_provenance"] = {
                            "source_index": str(source_path),
                            "source_index_sha256": source_sha,
                            "source_run_id": source.get("run_id"),
                            "prior_source_provenance": copy.deepcopy(
                                prior_provenance
                            ),
                        }
                        self.c1_operational_overhead.append(imported_overhead)
                    elif (
                        (
                            key in (
                                preserved_model_failure_keys
                                | preserved_operational_failure_keys
                            )
                            and source_rows.get(key, {}).get("status") == "error"
                        )
                        or (
                            source_rows.get(key, {}).get("status") == "ok"
                            and isinstance(
                                source_rows.get(key, {}).get("replacement_for"), dict
                            )
                        )
                    ):
                        raw_report = overhead.get("report")
                        report_path = (
                            Path(str(raw_report)).expanduser()
                            if isinstance(raw_report, str) and raw_report
                            else None
                        )
                        if (
                            report_path is None
                            or not report_path.is_file()
                            or hashlib.sha256(report_path.read_bytes()).hexdigest()
                            != overhead.get("report_sha256")
                        ):
                            raise ValueError(
                                "C1 flattened terminal overhead evidence mismatch"
                            )
                        imported_overhead = copy.deepcopy(overhead)
                        imported_overhead["source_provenance"] = {
                            "source_index": str(source_path),
                            "source_index_sha256": source_sha,
                            "source_run_id": source.get("run_id"),
                        }
                        self.c1_operational_overhead.append(imported_overhead)
                    elif (
                        source_rows.get(key, {}).get("status")
                        not in {"error", "quarantined_unknown_usage"}
                        and key not in preserved_model_failure_keys
                    ):
                        raise ValueError(
                            "C1 flattened local overhead is outside preserved outcomes"
                        )
        elif (
            set(overhead_by_key) != quarantine_keys
            or any(len(rows) != 1 for rows in overhead_by_key.values())
        ):
            raise ValueError(
                "C1 source operational-overhead tuples do not exactly match quarantine tuples"
            )
        if imported_model_failure_keys != (
            preserved_model_failure_keys | inherited_model_failure_keys
        ):
            raise ValueError(
                "registered preserved C1 model-failure tuple set does not match "
                "the source evidence"
            )
        if imported_operational_failure_keys != preserved_operational_failure_keys:
            raise ValueError(
                "registered preserved C1 operational-failure tuple set does not match"
            )
        local_overhead_keys = {
            self._trial_key(
                str(row.get("arm") or ""),
                row.get("model"),
                row.get("seed"),
                str(row.get("task_id") or ""),
            )
            for row in self.c1_operational_overhead
            if row.get("source_provenance") is None
        }
        if not local_overhead_keys.issubset(replacement_keys):
            raise ValueError("recovery root contains overhead outside replacement tuples")
        for arm, row in completed_rows:
            rows = self.index.setdefault(arm, [])
            rows[:] = [
                existing
                for existing in rows
                if not (
                    existing.get("model") == row.get("model")
                    and existing.get("seed") == row.get("seed")
                    and existing.get("task_id") == row.get("task_id")
                )
            ]
            rows.append(row)
        imported_pair_start_entries = 0
        source_pair_start = (
            source_cache_policy.get("pair_start")
            if isinstance(source_cache_policy, dict)
            else None
        )
        if isinstance(source_pair_start, str) and source_pair_start:
            source_pair_root = Path(source_pair_start).expanduser().resolve()
            if not source_pair_root.is_relative_to(source_run_dir):
                raise ValueError("C1 source pair-Start cache escapes source run")
            target_pair_root = self.run_dir / "c1-pair-start-cache"
            if source_pair_root.is_dir():
                for source_entry in sorted(source_pair_root.glob("*/*.json")):
                    try:
                        pair_payload = json.loads(
                            source_entry.read_text(encoding="utf-8")
                        )
                    except Exception as error:
                        raise ValueError(
                            "C1 source pair-Start cache entry is unreadable"
                        ) from error
                    if (
                        not isinstance(pair_payload, dict)
                        or pair_payload.get("schema")
                        != "c1-paired-start-gate-v1"
                        or pair_payload.get("schema_version") != 1
                        or pair_payload.get("written_before_transition") is not True
                    ):
                        raise ValueError(
                            "C1 source pair-Start cache entry is invalid"
                        )
                    relative = source_entry.relative_to(source_pair_root)
                    destination = target_pair_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        if hashlib.sha256(destination.read_bytes()).hexdigest() != hashlib.sha256(
                            source_entry.read_bytes()
                        ).hexdigest():
                            raise ValueError(
                                "C1 recovery pair-Start cache entry conflicts"
                            )
                    else:
                        shutil.copy2(source_entry, destination)
                    imported_pair_start_entries += 1
        self.c1_sources.append(
            {
                "source_index": str(source_path),
                "source_index_sha256": source_sha,
                "source_run_id": source.get("run_id"),
                "completed_trials": len(completed_rows),
                "replacement_trials": len(replacement_keys),
                "quarantined_trials": len(quarantine_keys),
                "exact_usage_timeout_trials": len(exact_timeout_keys),
                "exact_usage_retryable_infrastructure_trials": len(
                    retryable_infrastructure_keys
                ),
                "imported_nonretryable_model_failure_trials": len(
                    imported_model_failure_keys
                ),
                "imported_terminal_operational_failure_trials": len(
                    imported_operational_failure_keys
                ),
                "imported_pair_start_entries": imported_pair_start_entries,
                "contract_migration": copy.deepcopy(
                    self.c1_source_contract_migration_metadata
                ),
            }
        )
        self._c1_source_imported = True
        with self._index_lock:
            self._write_index_unlocked()

    # -- arms --------------------------------------------------------------------

    def _run_c1_state_invocation(
        self, model: str, seed: int, *, arm: str = "c1_state"
    ) -> None:
        if arm not in {"c1_state", "c1_state_minimal_agent"}:
            raise ValueError(f"unsupported state arm {arm!r}")
        cfg = self._arm_cfg(arm)
        minimal = arm == "c1_state_minimal_agent"
        transition_action_source = (
            str(cfg.get("transition_action_source") or "minimal_agent_v1")
            if minimal
            else "generated_spec"
        )
        shared_synthesis_cache = (
            self.run_dir / "c1-self-synthesis-cache"
        ).resolve()
        minimal_action_cache = (
            self.run_dir / "c1-minimal-action-cache"
        ).resolve()
        source_action_cache = (
            self.run_dir / "c1-source-action-cache"
        ).resolve()
        pair_start_cache = (
            self.run_dir / "c1-pair-start-cache"
        ).resolve()
        pair_start_enabled = bool(
            isinstance(self.c1_start_gate_protocol, dict)
            and isinstance(
                self.c1_start_gate_protocol.get("pair_start_gate"), dict
            )
        )

        def transition_invocation_config() -> dict[str, Any]:
            if not minimal:
                return {}
            if transition_action_source == "source_agent_v2":
                return {
                    "transition_action_source": transition_action_source,
                    "source_action_cache": str(source_action_cache),
                    "source_action_max_steps": int(
                        cfg.get("source_action_max_steps", 16)
                    ),
                    "source_action_max_tokens": int(
                        cfg.get("source_action_max_tokens", 8192)
                    ),
                    "source_action_format_repairs": int(
                        cfg.get("source_action_format_repairs", 1)
                    ),
                    "source_action_prompt_revision": str(
                        cfg.get("source_action_prompt_revision") or ""
                    ),
                }
            return {
                "transition_action_source": transition_action_source,
                "minimal_action_cache": str(minimal_action_cache),
                "minimal_action_max_actions": int(
                    cfg.get("minimal_action_max_actions", 8)
                ),
                "minimal_action_max_tokens": int(
                    cfg.get("minimal_action_max_tokens", 2048)
                ),
                "minimal_action_format_repairs": int(
                    cfg.get("minimal_action_format_repairs", 1)
                ),
            }

        transition_config = transition_invocation_config()
        if self.c1_trial_persistence is not None and self.c1_source_index is None:
            task_ids = [str(task["id"]) for task in self._c1_task_records(cfg)]
            if any(
                self._quarantined_trial(
                    arm, model=model, seed=seed, task_id=task_id
                )
                for task_id in task_ids
            ):
                raise QuarantinedC1TrialsError(
                    "same-root resume is forbidden after an unknown-usage trial; "
                    "use a new clean root with --c1-source-index"
                )
            if task_ids and all(
                self._completed(
                    arm, model=model, seed=seed, task_id=task_id
                )
                for task_id in task_ids
            ):
                return
        if self.c1_trial_persistence is not None and self.c1_source_index is not None:
            recovery_tasks = []
            attempts: dict[str, int] = {}
            for task in self._c1_task_records(cfg):
                task_id = str(task["id"])
                attempt = self._recovery_trial_attempt(
                    arm, model=model, seed=seed, task_id=task_id
                )
                if attempt is not None:
                    recovery_tasks.append(task)
                    attempts[task_id] = attempt

            def execute_recovery_task(
                task: dict[str, Any],
                ticket: Any,
                lane_id: int,
                recovery_handle: Any,
            ) -> str | None:
                del lane_id
                task_id = ticket.task_id
                attempt = attempts[task_id]
                tasks_path = self._c1_trial_tasks_path(cfg, task)
                arm_dir = self._c1_trial_dir(
                    arm, model, seed, task_id, attempt=attempt
                )
                replacement_for = self._c1_replacement_sources.get(
                    self._trial_key(arm, model, seed, task_id)
                )
                return self._execute(
                    arm,
                        model=model,
                        seed=seed,
                        task_id=task_id,
                        attempt=attempt,
                        invocation_dir=arm_dir,
                        tasks_path=tasks_path,
                        invocation={
                            "c1_outer_worker": self.c1_outer_worker,
                            "c1_inner_worker": self.c1_inner_worker,
                            "inner_task_plan": self._c1_inner_task_plan([task]),
                            "unity_serialized": self.c1_unity_serialized,
                            "unity_serialized_benchmarks": list(
                                self.c1_unity_serialized_benchmarks
                            ),
                            "background_browser_headless": True,
                            "explore": bool(cfg.get("explore", True)),
                            "synthesis_ablation": str(cfg.get("synthesis_ablation", "full")),
                            "max_explore_steps": int(
                                cfg.get("max_explore_steps", 12)
                            ),
                            "shared_synthesis_cache": str(
                                shared_synthesis_cache
                            ),
                            **(
                                {"pair_start_cache": str(pair_start_cache)}
                                if pair_start_enabled
                                else {}
                            ),
                            **transition_config,
                            "max_iters": int(cfg.get("max_iters", 1)),
                            "seeds": 1,
                            "restore_diagnostics": bool(
                                cfg.get("restore_diagnostics", False)
                            ),
                            "restore_diagnostic_task_ids": (
                                [task_id]
                                if bool(cfg.get("restore_diagnostics", False))
                                else []
                            ),
                            "task_timeout_s": self.c1_task_timeout_s,
                            "atomic_defer": True,
                        },
                        replacement_for=replacement_for,
                        fn=lambda rm, path=tasks_path, directory=arm_dir: run_state_loop_benchmark(
                            self.base,
                            tasks_path=path,
                            agent_kind=self.agent_kind,
                            model=model,
                            base_url=self.base_url,
                            api_key_env=self.api_key_env,
                            temperature=self.temperature,
                            explore=bool(cfg.get("explore", True)),
                            synthesis_ablation=str(cfg.get("synthesis_ablation", "full")),
                            max_explore_steps=int(
                                cfg.get("max_explore_steps", 12)
                            ),
                            shared_synthesis_cache=shared_synthesis_cache,
                            arm=arm,
                            transition_action_source=transition_action_source,
                            minimal_action_max_actions=int(
                                cfg.get("minimal_action_max_actions", 8)
                            ),
                            minimal_action_max_tokens=int(
                                cfg.get("minimal_action_max_tokens", 2048)
                            ),
                            minimal_action_format_repairs=int(
                                cfg.get("minimal_action_format_repairs", 1)
                            ),
                            minimal_action_cache=(
                                minimal_action_cache if minimal else None
                            ),
                            source_action_max_steps=int(
                                cfg.get("source_action_max_steps", 16)
                            ),
                            source_action_max_tokens=int(
                                cfg.get("source_action_max_tokens", 8192)
                            ),
                            source_action_format_repairs=int(
                                cfg.get("source_action_format_repairs", 1)
                            ),
                            source_action_cache=(
                                source_action_cache
                                if transition_action_source == "source_agent_v2"
                                else None
                            ),
                            source_action_planner_factory=(
                                DeterministicSourceTransitionActionAgent
                                if transition_action_source == "source_agent_v2"
                                and self.agent_kind != "llm"
                                else None
                            ),
                            pair_start_cache=(
                                pair_start_cache if pair_start_enabled else None
                            ),
                            max_iters=int(cfg.get("max_iters", 1)),
                            seeds=1,
                            seed=seed,
                            resume=True,
                            headless=self.headless,
                            background_browser_headless=True,
                            report_dir=directory,
                            run_meta=rm,
                            restore_diagnostics=bool(
                                cfg.get("restore_diagnostics", False)
                            ),
                            restore_diagnostic_task_ids=(
                                [task_id]
                                if bool(cfg.get("restore_diagnostics", False))
                                else None
                            ),
                            inner_workers=self.c1_inner_worker,
                            sandbox_manager=self.c1_sandbox_manager,
                            invocation_id=(
                                f"{arm}-{run_context.manifest_hash(model)}-seed{seed}-"
                                f"{task_id}-attempt{attempt}"
                            ),
                            unity_serialized=self.c1_unity_serialized,
                            unity_serialized_benchmarks=(
                                self.c1_unity_serialized_benchmarks
                            ),
                            expected_task_plan=self._c1_inner_task_plan([task]),
                            ready_scheduler=self.c1_ready_scheduler,
                            ready_outer_ordinal=int(
                                (getattr(self._latin_local, "value", None) or {}).get(
                                    "outer_ordinal", 0
                                )
                            ),
                            global_stop_event=self._c1_global_stop_event,
                            task_timeout_s=self.c1_task_timeout_s,
                            ready_handle=recovery_handle,
                            ready_ticket=ticket,
                            atomic_defer=True,
                        ),
                )

            self._run_c1_recovery_invocation(
                invocation_id=(
                    f"recovery-{'c1-state' if arm == 'c1_state' else arm}-"
                    f"{run_context.manifest_hash(model)}-seed{seed}"
                ),
                recovery_tasks=recovery_tasks,
                execute_task=execute_recovery_task,
            )
            return
        tasks_path = self._c1_tasks_path(cfg)
        arm_dir = self.run_dir / arm / run_context.manifest_hash(model) / f"seed{seed}"
        self._execute(
            arm, model=model, seed=seed,
            invocation_dir=arm_dir,
            tasks_path=tasks_path,
            invocation={
                "c1_outer_worker": self.c1_outer_worker,
                "c1_inner_worker": self.c1_inner_worker,
                "inner_task_plan": self._c1_inner_task_plan(
                    self._c1_task_records(cfg)
                ),
                "unity_serialized": self.c1_unity_serialized,
                "unity_serialized_benchmarks": list(
                    self.c1_unity_serialized_benchmarks
                ),
                "background_browser_headless": True,
                "explore": bool(cfg.get("explore", True)),
                "synthesis_ablation": str(cfg.get("synthesis_ablation", "full")),
                "max_explore_steps": int(cfg.get("max_explore_steps", 12)),
                "shared_synthesis_cache": str(shared_synthesis_cache),
                **(
                    {"pair_start_cache": str(pair_start_cache)}
                    if pair_start_enabled
                    else {}
                ),
                **transition_config,
                "max_iters": int(cfg.get("max_iters", 1)),
                "seeds": 1,
                "restore_diagnostics": bool(
                    cfg.get("restore_diagnostics", False)
                ),
                "restore_diagnostic_task_ids": (
                    [str(task["id"]) for task in self._c1_task_records(cfg)]
                    if bool(cfg.get("restore_diagnostics", False))
                    else []
                ),
                "task_timeout_s": self.c1_task_timeout_s,
            },
            fn=lambda rm: run_state_loop_benchmark(
                self.base, tasks_path=tasks_path, agent_kind=self.agent_kind, model=model,
                base_url=self.base_url, api_key_env=self.api_key_env,
                temperature=self.temperature,
                explore=bool(cfg.get("explore", True)),
                synthesis_ablation=str(cfg.get("synthesis_ablation", "full")),
                max_explore_steps=int(cfg.get("max_explore_steps", 12)),
                shared_synthesis_cache=shared_synthesis_cache,
                arm=arm,
                transition_action_source=transition_action_source,
                minimal_action_max_actions=int(
                    cfg.get("minimal_action_max_actions", 8)
                ),
                minimal_action_max_tokens=int(
                    cfg.get("minimal_action_max_tokens", 2048)
                ),
                minimal_action_format_repairs=int(
                    cfg.get("minimal_action_format_repairs", 1)
                ),
                minimal_action_cache=minimal_action_cache if minimal else None,
                source_action_max_steps=int(
                    cfg.get("source_action_max_steps", 16)
                ),
                source_action_max_tokens=int(
                    cfg.get("source_action_max_tokens", 8192)
                ),
                source_action_format_repairs=int(
                    cfg.get("source_action_format_repairs", 1)
                ),
                source_action_cache=(
                    source_action_cache
                    if transition_action_source == "source_agent_v2"
                    else None
                ),
                source_action_planner_factory=(
                    DeterministicSourceTransitionActionAgent
                    if transition_action_source == "source_agent_v2"
                    and self.agent_kind != "llm"
                    else None
                ),
                pair_start_cache=(
                    pair_start_cache if pair_start_enabled else None
                ),
                max_iters=int(cfg.get("max_iters", 1)),
                seeds=1, seed=seed, resume=True,
                headless=self.headless, report_dir=arm_dir, run_meta=rm,
                background_browser_headless=True,
                restore_diagnostics=bool(cfg.get("restore_diagnostics", False)),
                restore_diagnostic_task_ids=(
                    [str(task["id"]) for task in self._c1_task_records(cfg)]
                    if bool(cfg.get("restore_diagnostics", False))
                    else None
                ),
                inner_workers=self.c1_inner_worker,
                sandbox_manager=self.c1_sandbox_manager,
                invocation_id=f"{arm}-{run_context.manifest_hash(model)}-seed{seed}",
                unity_serialized=self.c1_unity_serialized,
                unity_serialized_benchmarks=(
                    self.c1_unity_serialized_benchmarks
                ),
                expected_task_plan=self._c1_inner_task_plan(
                    self._c1_task_records(cfg)
                ),
                ready_scheduler=self.c1_ready_scheduler,
                ready_outer_ordinal=int(
                    (getattr(self._latin_local, "value", None) or {}).get(
                        "outer_ordinal", 0
                    )
                ),
                global_stop_event=self._c1_global_stop_event,
                task_timeout_s=self.c1_task_timeout_s,
            ),
        )

    def run_c1_state(self) -> None:
        for model in self.models:
            for seed in range(self.seeds):
                self._run_c1_state_invocation(model, seed)

    def run_c1_state_minimal_agent(self) -> None:
        self._require_shared_synthesis_parity(
            ["c1_state", "c1_state_minimal_agent"]
        )
        self._register_c1_trial_matrix(["c1_state_minimal_agent"])
        self._import_c1_source_trials()
        for model in self.models:
            for seed in range(self.seeds):
                self._run_c1_state_invocation(
                    model, seed, arm="c1_state_minimal_agent"
                )

    def run_c1_gui(self) -> None:
        self._run_gui_arm("c1_gui", start_mode="from_start")

    def run_c1_gui_synth(self) -> None:
        self._run_gui_arm("c1_gui_synth", start_mode="synthesized")

    def run_c1_agent_s(self) -> None:
        self._run_gui_arm("c1_agent_s", start_mode="from_start")

    def run_c1_agent_s_synth(self) -> None:
        self._run_gui_arm("c1_agent_s_synth", start_mode="synthesized")

    def run_c1_agent_scalecua(self) -> None:
        self._run_gui_arm("c1_agent_scalecua", start_mode="from_start")

    def run_c1_agent_scalecua_synth(self) -> None:
        self._run_gui_arm("c1_agent_scalecua_synth", start_mode="synthesized")

    def _run_gui_arm(self, arm: str, *, start_mode: str) -> None:
        self._require_calibration_gate()
        for model in self.models:
            for seed in range(self.seeds):
                self._run_gui_invocation(arm, start_mode=start_mode, model=model, seed=seed)

    def _run_gui_invocation(self, arm: str, *, start_mode: str, model: str, seed: int) -> None:
        self._require_calibration_gate()
        cfg = self._arm_cfg(arm)
        if self.c1_trial_persistence is not None and self.c1_source_index is None:
            task_ids = [str(task["id"]) for task in self._c1_task_records(cfg)]
            if any(
                self._quarantined_trial(
                    arm, model=model, seed=seed, task_id=task_id
                )
                for task_id in task_ids
            ):
                raise QuarantinedC1TrialsError(
                    "same-root resume is forbidden after an unknown-usage trial; "
                    "use a new clean root with --c1-source-index"
                )
            if task_ids and all(
                self._completed(arm, model=model, seed=seed, task_id=task_id)
                for task_id in task_ids
            ):
                return
        if not os.environ.get(self.api_key_env):
            if not self._completed(arm, model=model, seed=seed):
                self._record(
                    arm, model=model, seed=seed, status="skipped", report=None,
                    history=[], error=f"{self.api_key_env} not set; real-vision GUI baseline needs an API key",
                )
            print(f"[p5] skip {arm} model={model} seed={seed} (no API key)", flush=True)
            if self.agent_kind == "llm":
                raise RuntimeError(f"{self.api_key_env} not set; formal C1 run paused")
            return
        tasks_path = self._c1_tasks_path(cfg)
        arm_start_mode = str(cfg.get("start_mode", start_mode))
        policy_kind = str(cfg.get("policy_kind") or ("agent_s" if arm.startswith("c1_agent_s") else "gui_agent"))
        step_budgets = {str(key): int(value) for key, value in (cfg.get("step_budgets") or {}).items()}
        bridge_cfg = self.manifest.get("visual_bridge") if isinstance(self.manifest.get("visual_bridge"), dict) else {}
        observer_model = str(bridge_cfg.get("observer_model") or "claude-sonnet-5@relay")
        grounder_model = resolve_grounder_model(
            bridge_cfg.get("grounder_model"),
            observer_model=observer_model,
            decision_model=model,
        )
        arm_dir = self.run_dir / arm / run_context.manifest_hash(model) / f"seed{seed}"
        shared_synthesis_cache = (
            self.run_dir / "c1-self-synthesis-cache"
        ).resolve()
        if self.c1_trial_persistence is not None and self.c1_source_index is not None:
            recovery_tasks = []
            attempts: dict[str, int] = {}
            for task in self._c1_task_records(cfg):
                task_id = str(task["id"])
                attempt = self._recovery_trial_attempt(
                    arm, model=model, seed=seed, task_id=task_id
                )
                if attempt is not None:
                    recovery_tasks.append(task)
                    attempts[task_id] = attempt

            def execute_recovery_task(
                task: dict[str, Any],
                ticket: Any,
                lane_id: int,
                recovery_handle: Any,
            ) -> str | None:
                del lane_id
                task_id = ticket.task_id
                attempt = attempts[task_id]
                trial_tasks_path = self._c1_trial_tasks_path(cfg, task)
                trial_dir = self._c1_trial_dir(
                    arm, model, seed, task_id, attempt=attempt
                )
                replacement_for = self._c1_replacement_sources.get(
                    self._trial_key(arm, model, seed, task_id)
                )
                return self._execute(
                    arm,
                        model=model,
                        seed=seed,
                        task_id=task_id,
                        attempt=attempt,
                        invocation_dir=trial_dir,
                        tasks_path=trial_tasks_path,
                        replacement_for=replacement_for,
                        invocation={
                            "c1_outer_worker": self.c1_outer_worker,
                            "c1_inner_worker": self.c1_inner_worker,
                            "inner_task_plan": self._c1_inner_task_plan([task]),
                            "unity_serialized": self.c1_unity_serialized,
                            "unity_serialized_benchmarks": list(
                                self.c1_unity_serialized_benchmarks
                            ),
                            "background_browser_headless": True,
                            "start_mode": arm_start_mode,
                            "policy_kind": policy_kind,
                            "step_budgets": step_budgets,
                            "observer_model": observer_model,
                            "grounder_model": grounder_model,
                            "provider_timeout_s": self.provider_timeout_s,
                            "task_timeout_s": self.c1_task_timeout_s,
                            "synth_model": model,
                            "max_explore_steps": int(cfg.get("max_explore_steps", 12)),
                            "shared_synthesis_cache": str(shared_synthesis_cache),
                            "start_gate_protocol": self.c1_start_gate_protocol,
                            "atomic_defer": True,
                        },
                        fn=lambda rm, path=trial_tasks_path, directory=trial_dir: run_gui_agent_benchmark(
                            self.base,
                            tasks_path=path,
                            model=model,
                            base_url=self.base_url,
                            api_key_env=self.api_key_env,
                            temperature=self.temperature,
                            start_mode=arm_start_mode,
                            policy_kind=policy_kind,
                            seed=seed,
                            step_budgets=step_budgets,
                            resume=True,
                            observer_model=observer_model,
                            grounder_model=grounder_model,
                            provider_timeout_s=self.provider_timeout_s,
                            synth_model=model,
                            max_explore_steps=int(cfg.get("max_explore_steps", 12)),
                            shared_synthesis_cache=shared_synthesis_cache,
                            headless=self.headless,
                            background_browser_headless=True,
                            report_dir=directory,
                            run_meta=rm,
                            inner_workers=self.c1_inner_worker,
                            sandbox_manager=self.c1_sandbox_manager,
                            invocation_id=(
                                f"{arm}-{run_context.manifest_hash(model)}-seed{seed}-"
                                f"{task_id}-attempt{attempt}"
                            ),
                            unity_serialized=self.c1_unity_serialized,
                            unity_serialized_benchmarks=(
                                self.c1_unity_serialized_benchmarks
                            ),
                            expected_task_plan=self._c1_inner_task_plan([task]),
                            ready_scheduler=self.c1_ready_scheduler,
                            ready_outer_ordinal=int(
                                (getattr(self._latin_local, "value", None) or {}).get(
                                    "outer_ordinal", 0
                                )
                            ),
                            global_stop_event=self._c1_global_stop_event,
                            task_timeout_s=self.c1_task_timeout_s,
                            ready_handle=recovery_handle,
                            ready_ticket=ticket,
                            atomic_defer=True,
                        ),
                )

            self._run_c1_recovery_invocation(
                invocation_id=(
                    f"recovery-{arm}-{run_context.manifest_hash(model)}-seed{seed}"
                ),
                recovery_tasks=recovery_tasks,
                execute_task=execute_recovery_task,
            )
            return
        self._execute(
            arm, model=model, seed=seed,
            invocation_dir=arm_dir,
            tasks_path=tasks_path,
            invocation={
                "c1_outer_worker": self.c1_outer_worker,
                "c1_inner_worker": self.c1_inner_worker,
                "inner_task_plan": self._c1_inner_task_plan(
                    self._c1_task_records(cfg)
                ),
                "unity_serialized": self.c1_unity_serialized,
                "unity_serialized_benchmarks": list(
                    self.c1_unity_serialized_benchmarks
                ),
                "background_browser_headless": True,
                "start_mode": arm_start_mode,
                "policy_kind": policy_kind,
                "step_budgets": step_budgets,
                "observer_model": observer_model,
                "grounder_model": grounder_model,
                "provider_timeout_s": self.provider_timeout_s,
                "task_timeout_s": self.c1_task_timeout_s,
                "synth_model": model,
                "max_explore_steps": int(cfg.get("max_explore_steps", 12)),
                "shared_synthesis_cache": str(shared_synthesis_cache),
                "start_gate_protocol": self.c1_start_gate_protocol,
            },
            fn=lambda rm: run_gui_agent_benchmark(
                self.base, tasks_path=tasks_path, model=model, base_url=self.base_url,
                api_key_env=self.api_key_env, temperature=self.temperature,
                start_mode=arm_start_mode, policy_kind=policy_kind, seed=seed,
                step_budgets=step_budgets, resume=True,
                observer_model=observer_model, grounder_model=grounder_model,
                provider_timeout_s=self.provider_timeout_s,
                synth_model=model, max_explore_steps=int(cfg.get("max_explore_steps", 12)),
                shared_synthesis_cache=shared_synthesis_cache,
                headless=self.headless, report_dir=arm_dir, run_meta=rm,
                background_browser_headless=True,
                inner_workers=self.c1_inner_worker,
                sandbox_manager=self.c1_sandbox_manager,
                invocation_id=f"{arm}-{run_context.manifest_hash(model)}-seed{seed}",
                unity_serialized=self.c1_unity_serialized,
                unity_serialized_benchmarks=(
                    self.c1_unity_serialized_benchmarks
                ),
                expected_task_plan=self._c1_inner_task_plan(
                    self._c1_task_records(cfg)
                ),
                ready_scheduler=self.c1_ready_scheduler,
                ready_outer_ordinal=int(
                    (getattr(self._latin_local, "value", None) or {}).get(
                        "outer_ordinal", 0
                    )
                ),
                global_stop_event=self._c1_global_stop_event,
                task_timeout_s=self.c1_task_timeout_s,
            ),
        )

    def _expected_c1_registration(
        self, selected_arms: list[str]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        base_order = list(C1_ARMS)
        selected = set(selected_arms)
        blocks: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        for model_index, model in enumerate(self.models):
            for seed in range(self.seeds):
                offset = (model_index * self.seeds + seed) % len(base_order)
                cyclic = base_order[offset:] + base_order[:offset]
                ordered = [arm for arm in cyclic if arm in selected]
                blocks.append(
                    {
                        "model": model,
                        "model_index": model_index,
                        "seed": seed,
                        "offset": offset,
                        "arms": ordered,
                    }
                )
                for position, arm in enumerate(ordered):
                    rows.append(
                        {
                            "ordinal": len(rows),
                            "arm": arm,
                            "model": model,
                            "seed": seed,
                            "model_index": model_index,
                            "offset": offset,
                            "position": position,
                            "ordered_arms": ordered,
                            "status": "registered",
                            "dispatch_history": [],
                        }
                    )
        return (
            {
                "base_order": base_order,
                "offset_rule": (
                    f"(model_index * seeds + seed) mod {len(base_order)}"
                ),
                "blocks": blocks,
            },
            rows,
        )

    def _validate_c1_registration(
        self,
        selected_arms: list[str],
        *,
        allow_empty: bool,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        expected_latin, expected_rows = self._expected_c1_registration(selected_arms)
        if not self.execution_order and allow_empty:
            return expected_latin, expected_rows
        actual_identities = [
            self._execution_identity(row) for row in self.execution_order
        ]
        expected_identities = [self._execution_identity(row) for row in expected_rows]
        if actual_identities != expected_identities:
            raise ValueError(
                "Cannot resume C1 with changed canonical execution registration"
            )
        if self.latin_square != expected_latin:
            raise ValueError("Cannot resume C1 with changed Latin-square registration")
        return expected_latin, expected_rows

    def _run_c1_latin_square(self, selected_arms: list[str]) -> None:
        """Execute model×seed blocks with the cyclic five-arm Latin-square order."""

        preflight_cfg = self.manifest.get("preflight")
        if (
            self.agent_kind == "llm"
            and isinstance(preflight_cfg, dict)
            and preflight_cfg.get("required") is True
            and not self.provider_timeout_registered
        ):
            raise RuntimeError(
                "Formal C1 manifest must register api.provider_timeout_s before paid calls"
            )
        self._require_preflight_gate()
        if any(arm != "c1_state" for arm in selected_arms):
            self._require_calibration_gate()
        self._require_shared_synthesis_parity(selected_arms)
        self._register_c1_trial_matrix(selected_arms)
        existing_quarantine = [
            TrialKey.from_row(arm, row)
            for arm in selected_arms
            for row in self.index.get(arm, [])
            if row.get("status") == "quarantined_unknown_usage"
        ]
        if existing_quarantine and (
            self.c1_source_index is None or not self.resume
        ):
            raise QuarantinedC1TrialsError(
                "same-attempt C1 resume is forbidden after unknown usage; no new "
                "policy call was made. An original run needs a new clean recovery "
                "root; a recovery run needs an explicit --resume for its next "
                "registered fresh attempt."
            )
        self._import_c1_source_trials()
        start_modes = {
            "c1_gui": "from_start",
            "c1_gui_synth": "synthesized",
            "c1_agent_s": "from_start",
            "c1_agent_s_synth": "synthesized",
            "c1_agent_scalecua": "from_start",
            "c1_agent_scalecua_synth": "synthesized",
        }
        expected_latin, expected_rows = self._validate_c1_registration(
            selected_arms,
            allow_empty=True,
        )
        restored_registration = bool(
            self._resume_index_loaded and self.execution_order
        )
        if not self.execution_order:
            self.execution_order = expected_rows
            self.latin_square = expected_latin

        def invocation_evidence_complete(arm: str, model: str, seed: int) -> bool:
            if self.c1_trial_persistence is None:
                return self._completed(arm, model=model, seed=seed)
            task_ids = [
                str(task["id"])
                for task in self._c1_task_records(self._arm_cfg(arm))
            ]
            return bool(task_ids) and all(
                self._completed(arm, model=model, seed=seed, task_id=task_id)
                for task_id in task_ids
            )

        jobs: list[tuple[int, str, str, int, dict[str, Any]]] = []
        for row in self.execution_order:
            ordinal = int(row["ordinal"])
            arm = str(row["arm"])
            model = str(row["model"])
            seed = int(row["seed"])
            status = str(row.get("status") or "registered")
            complete = invocation_evidence_complete(arm, model, seed)
            if restored_registration and status in {"complete", "imported_complete"}:
                if not complete:
                    raise ValueError(
                        "Completed C1 registration no longer has valid immutable evidence"
                    )
                continue
            if restored_registration and row.get("integrity_failure") is True:
                raise ValueError(
                    "Cannot resume C1 after a shared integrity failure; use a new run"
                )
            if self.c1_source_index is not None and complete:
                row.update(
                    status="imported_complete",
                    source_provenance=copy.deepcopy(self.c1_sources),
                )
                continue
            if restored_registration and complete:
                # A crash can occur after immutable trial snapshots are written
                # but before the outer row reaches its terminal status. Reconcile
                # from those validated snapshots without a fake producer dispatch.
                row.update(status="complete", reconciled_from_evidence=True)
                continue
            active_latin = {
                field: copy.deepcopy(row[field])
                for field in (
                    "model_index",
                    "seed",
                    "offset",
                    "position",
                    "ordered_arms",
                )
            }
            jobs.append((ordinal, arm, model, seed, active_latin))

        failures: list[BaseException] = []
        global_stop = C1GlobalStop()
        self._c1_global_stop_event = global_stop

        def shared_integrity_failure(error: BaseException) -> bool:
            if isinstance(error, ValueError):
                return True
            message = str(error).lower()
            return any(
                marker in message
                for marker in (
                    "harness changed",
                    "provenance",
                    "manifest changed",
                    "attempt ledger hash mismatch",
                    "preflight artifact changed",
                    "calibration artifact changed",
                    "source contract migration artifact changed",
                )
            )

        def fail_closed_stop_reason(error: BaseException) -> str | None:
            if shared_integrity_failure(error):
                return "shared C1 integrity failure"
            if isinstance(error, cost.UsageAccountingError) and (
                "permanently unknown provider usage" in str(error).lower()
            ):
                return "permanently unknown provider usage"
            return None

        def run_job(job: tuple[int, str, str, int, dict[str, Any]]) -> None:
            ordinal, arm, model, seed, active_latin = job
            active_latin["outer_ordinal"] = ordinal
            self._latin_local.value = active_latin
            dispatch: dict[str, Any] | None = None
            with self._index_lock:
                if global_stop.is_set():
                    reason = global_stop.reason or "shared C1 integrity failure"
                    integrity_failure = reason == "shared C1 integrity failure"
                    self.execution_order[ordinal].update(
                        status=(
                            "cancelled_global_integrity"
                            if integrity_failure
                            else "cancelled_global_unknown_usage"
                        ),
                        cancellation_reason=reason,
                        integrity_failure=integrity_failure,
                    )
                    self._write_index_unlocked()
                    self._latin_local.value = None
                    return
                started = self._outer_started
                self._outer_started += 1
                self._outer_active += 1
                self.c1_outer_scheduler["max_active_invocations"] = max(
                    int(self.c1_outer_scheduler["max_active_invocations"]),
                    self._outer_active,
                )
                history = self.execution_order[ordinal].setdefault(
                    "dispatch_history", []
                )
                if not isinstance(history, list):
                    raise ValueError("C1 dispatch_history is not a list")
                dispatch = {
                    "dispatch_ordinal": len(history),
                    "start_ordinal": started,
                    "started_monotonic_ns": time.monotonic_ns(),
                    "status": "running",
                }
                history.append(dispatch)
                for stale_field in (
                    "completion_ordinal",
                    "completed_monotonic_ns",
                    "cancellation_reason",
                    "error",
                    "integrity_failure",
                    "reconciled_from_evidence",
                ):
                    self.execution_order[ordinal].pop(stale_field, None)
                self.execution_order[ordinal].update(
                    start_ordinal=dispatch["start_ordinal"],
                    started_monotonic_ns=dispatch["started_monotonic_ns"],
                    status="running",
                )
            try:
                if arm == "c1_state":
                    self._run_c1_state_invocation(model, seed)
                else:
                    self._run_gui_invocation(
                        arm,
                        start_mode=start_modes[arm],
                        model=model,
                        seed=seed,
                    )
            except BaseException as error:
                stop_reason = fail_closed_stop_reason(error)
                integrity_failure = stop_reason == "shared C1 integrity failure"
                with self._index_lock:
                    if stop_reason is not None:
                        global_stop.request(stop_reason)
                    self.execution_order[ordinal].update(
                        status="failed",
                        integrity_failure=integrity_failure,
                        error=run_context.safe_error_text(error),
                    )
                    assert dispatch is not None
                    dispatch.update(
                        status="failed",
                        integrity_failure=integrity_failure,
                        error=run_context.safe_error_text(error),
                    )
                raise
            else:
                with self._index_lock:
                    assert dispatch is not None
                    if global_stop.is_set() and not invocation_evidence_complete(
                        arm, model, seed
                    ):
                        reason = global_stop.reason or "shared C1 integrity failure"
                        integrity_failure = (
                            reason == "shared C1 integrity failure"
                        )
                        status = (
                            "cancelled_global_integrity"
                            if integrity_failure
                            else "cancelled_global_unknown_usage"
                        )
                        self.execution_order[ordinal].update(
                            status=status,
                            cancellation_reason=reason,
                            integrity_failure=integrity_failure,
                        )
                        dispatch.update(
                            status=status,
                            cancellation_reason=reason,
                            integrity_failure=integrity_failure,
                        )
                    else:
                        self.execution_order[ordinal]["status"] = "complete"
                        dispatch["status"] = "complete"
            finally:
                if dispatch is not None:
                    with self._index_lock:
                        completed = self._outer_completed
                        self._outer_completed += 1
                        self._outer_active -= 1
                        dispatch.update(
                            completion_ordinal=completed,
                            completed_monotonic_ns=time.monotonic_ns(),
                        )
                        self.execution_order[ordinal].update(
                            completion_ordinal=dispatch["completion_ordinal"],
                            completed_monotonic_ns=dispatch[
                                "completed_monotonic_ns"
                            ],
                        )
                        self._write_index_unlocked()
                self._latin_local.value = None

        effective_workers = min(self.c1_outer_worker, len(jobs))
        self.c1_outer_scheduler["effective_outer_workers"] = max(
            int(self.c1_outer_scheduler.get("effective_outer_workers") or 0),
            effective_workers,
        )
        with self._index_lock:
            self._write_index_unlocked()
        if effective_workers == 0:
            pass
        elif effective_workers == 1:
            for job in jobs:
                try:
                    run_job(job)
                except BaseException as error:  # noqa: BLE001 - settle all invocations
                    failures.append(error)
        else:
            with ThreadPoolExecutor(max_workers=effective_workers) as pool:
                futures = [pool.submit(run_job, job) for job in jobs]
                for future in futures:
                    try:
                        future.result()
                    except BaseException as error:  # noqa: BLE001
                        failures.append(error)
        if failures:
            self._c1_global_stop_event = None
            raise failures[0]
        self._c1_global_stop_event = None
        if self._c1_quarantined:
            unique = {
                (
                    row.get("arm"),
                    row.get("model"),
                    row.get("seed"),
                    row.get("task_id"),
                )
                for row in self._c1_quarantined
            }
            raise QuarantinedC1TrialsError(
                f"{len(unique)} C1 trial(s) quarantined for permanently unknown usage; "
                "completed trials remain immutable. Use a new clean root with "
                "--c1-source-index to run exactly the registered replacements."
            )

    def run_c2_phase3(self) -> None:
        cfg = self._arm_cfg("c2_phase3")
        for model in self.models:
            arm_dir = self.run_dir / "c2_phase3" / run_context.manifest_hash(model)
            self._execute(
                "c2_phase3", model=model, seed=None,
                fn=lambda rm, m=model, d=arm_dir: run_phase3_suite(
                    self.base, split_path=cfg.get("split"), report_dir=d, run_meta=rm,
                    agent_kind=self.agent_kind, model=m, base_url=self.base_url, api_key_env=self.api_key_env,
                    temperature=self.temperature,
                    iterations=int(cfg.get("iterations", 2)), population=int(cfg.get("population", 2)),
                    headless=self.headless,
                ),
            )

    def run_c2_memory_baseline(self) -> None:
        cfg = self._arm_cfg("c2_memory_baseline")
        backends = list(cfg.get("baseline_backends", ["amem", "mem0"]))
        anchor = cfg.get("anchor_backend", "stateportal-anchor")
        if anchor not in backends:
            backends = backends + [anchor]
        jobs = [(model, seed) for model in self.models for seed in range(self.seeds)]

        def run_job(model: str, seed: int) -> None:
            arm_dir = self.run_dir / "c2_memory_baseline" / run_context.manifest_hash(model) / f"seed{seed}"
            self._execute(
                "c2_memory_baseline", model=model, seed=seed,
                fn=lambda rm, m=model, d=arm_dir: run_memory_baseline_suite(
                    self.base, split_path=cfg.get("split"), backends=backends, anchor_backend=anchor,
                    report_dir=d, run_meta=rm, agent_kind=self.agent_kind, model=m,
                    base_url=self.base_url, api_key_env=self.api_key_env,
                    temperature=self.temperature,
                    iterations=int(cfg.get("iterations", 2)), headless=self.headless,
                    # Retrying one P5 invocation must reuse its atomic task
                    # checkpoints even when the outer command itself was a fresh
                    # run.  The invocation sidecar above prevents accidentally
                    # adopting artifacts from any other run.
                    resume=self.resume or any(d.rglob("evo-progress.json")),
                ),
                invocation_dir=arm_dir,
                invocation={
                    "split": str(cfg.get("split")),
                    "baseline_backends": backends,
                    "anchor_backend": anchor,
                    "iterations": int(cfg.get("iterations", 2)),
                },
            )

        if self.memory_workers == 1 or len(jobs) <= 1:
            for model, seed in jobs:
                run_job(model, seed)
        else:
            with ThreadPoolExecutor(max_workers=min(self.memory_workers, len(jobs))) as pool:
                futures = [pool.submit(run_job, model, seed) for model, seed in jobs]
                for future in futures:
                    future.result()
        with self._index_lock:
            incomplete = [
                row
                for row in self.index.get("c2_memory_baseline", [])
                if row.get("status") != "ok"
            ]
        if incomplete:
            raise RuntimeError(
                "C2 memory baseline is incomplete; inspect p5-run-index.json and resume the same report directory"
            )
        self._freeze_c2_harness_hash()

    def run_c3_fuzz(self) -> None:
        from .fuzzing import FuzzConfig

        cfg = self._arm_cfg("c3_fuzz")
        tasks_path = cfg.get("tasks", str(STATE_FUZZ_TASKS))
        arm_model = cfg.get("model")
        seeds = self.seeds  # run-level seeds win over the arm-level default
        for seed in range(seeds):
            arm_dir = self.run_dir / "c3_fuzz" / f"seed{seed}"
            fuzz_config = FuzzConfig(seed=seed, **({"model": arm_model} if arm_model else {}))
            self._execute(
                "c3_fuzz", model=arm_model, seed=seed,
                fn=lambda rm, d=arm_dir, fc=fuzz_config: require_complete_c3_fuzz_result(
                    run_fuzz_benchmark(
                        self.base, tasks_path=tasks_path, headless=self.headless, report_dir=d, run_meta=rm, config=fc,
                    )
                ),
            )

    # -- driver ------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        runners = {
            "c1_state": self.run_c1_state,
            "c1_state_minimal_agent": self.run_c1_state_minimal_agent,
            "c1_gui": self.run_c1_gui,
            "c1_gui_synth": self.run_c1_gui_synth,
            "c1_agent_s": self.run_c1_agent_s,
            "c1_agent_s_synth": self.run_c1_agent_s_synth,
            "c1_agent_scalecua": self.run_c1_agent_scalecua,
            "c1_agent_scalecua_synth": self.run_c1_agent_scalecua_synth,
            "c2_phase3": self.run_c2_phase3,
            "c2_memory_baseline": self.run_c2_memory_baseline,
            "c3_fuzz": self.run_c3_fuzz,
        }
        cm = _fake_visual_env() if self.fake_visual else _manifest_visual_judge_env(self.manifest)
        selected_c1 = [arm for arm in self.arms if arm in C1_EXECUTION_ARMS]
        selected_formal_c1 = [arm for arm in self.arms if arm in C1_ARMS]
        if selected_formal_c1 and self._resume_index_loaded:
            # Registration drift must fail before sandbox preflight, Unity
            # canaries, worker construction, or any provider-facing gate.
            self._validate_c1_registration(selected_formal_c1, allow_empty=True)
        if selected_c1:
            # Persist the entire selected tuple universe before sandbox setup,
            # canaries, or the first paid trial.  Diagnostic arms used to be
            # registered only after the formal arms finished, which made a
            # clean-root recovery impossible when an earlier arm stopped: the
            # source index was structurally missing every diagnostic tuple.
            self._require_shared_synthesis_parity(selected_c1)
            self._register_c1_trial_matrix(selected_c1)
        cache_cm = (
            _formal_c1_cache_env(self.run_dir / "c1-artifact-cache")
            if selected_c1
            else contextlib.nullcontext()
        )
        selected_profiled_arm = bool(selected_c1) or "c2_memory_baseline" in self.arms
        request_profile_cm = (
            model_request_profiles_environment(self.model_request_profiles)
            if selected_profiled_arm
            else contextlib.nullcontext()
        )
        provider_timeout_cm = (
            provider_timeout_environment(self.provider_timeout_s)
            if selected_c1
            else contextlib.nullcontext()
        )
        with (
            cm,
            cache_cm,
            request_profile_cm,
            provider_timeout_cm,
            _runtime_lock_dir_env(
                self.run_dir / "runtime-locks",
                runtime_lock_policy=self.runtime_lock_policy,
            ),
        ):
            if selected_c1:
                cfg = self._arm_cfg(selected_c1[0])
                repo_values = {
                    str(task.get("repo"))
                    for task in self._c1_task_records(cfg)
                    if isinstance(task.get("repo"), str) and task.get("repo")
                }
                repos = sorted(
                    {
                        (
                            Path(value).expanduser().resolve()
                            if Path(value).is_absolute()
                            else (self.base / value).resolve()
                        )
                        for value in repo_values
                    },
                    key=str,
                )
                if repos:
                    self.c1_sandbox_preflight = preflight_c1_sandboxes(repos)
                    self.c1_sandbox_manager = C1SandboxManager(run_id=self.run_id)
                    self.c1_sandbox_preflight["stale_sandboxes_removed_at_start"] = (
                        self.c1_sandbox_manager.cleanup_stale()
                    )
                    unity_rows = {
                        str(task.get("benchmark") or "").lower(): (
                            Path(str(task["repo"])).expanduser().resolve()
                            if Path(str(task["repo"])).is_absolute()
                            else (self.base / str(task["repo"])).resolve()
                        )
                        for task in self._c1_task_records(cfg)
                        if str(task.get("benchmark") or "").lower()
                        in UNITY_BENCHMARKS
                        and isinstance(task.get("repo"), str)
                    }
                    if (
                        self.agent_kind == "llm"
                        and self.c1_inner_worker > 1
                        and unity_rows
                    ):
                        canaries: dict[str, Any] = {}
                        for benchmark, repo in sorted(unity_rows.items()):
                            try:
                                canaries[benchmark] = run_unity_concurrency_canary(
                                    benchmark=benchmark,
                                    source_repo=repo,
                                    manager=self.c1_sandbox_manager,
                                    report_dir=self.run_dir / "c1-unity-canary",
                                )
                            except BaseException as error:  # noqa: BLE001
                                canaries[benchmark] = {
                                    "passed": False,
                                    "error": run_context.safe_error_text(error),
                                }
                        (
                            self.c1_unity_serialized_benchmarks,
                            blocking_canaries,
                        ) = classify_unity_concurrency_canaries(
                            canaries
                        )
                        self.c1_unity_serialized = bool(
                            self.c1_unity_serialized_benchmarks
                        )
                        self.c1_unity_canary = {
                            "schema_version": 2,
                            "benchmarks": canaries,
                            "passed": not blocking_canaries,
                            "fully_parallel": not self.c1_unity_serialized
                            and not blocking_canaries,
                            "paid_recovery_safe": not blocking_canaries,
                            "blocking_benchmarks": blocking_canaries,
                            "serialized_benchmarks": list(
                                self.c1_unity_serialized_benchmarks
                            ),
                            "fallback": (
                                "unity_per_benchmark_serial"
                                if self.c1_unity_serialized
                                else None
                            ),
                        }
                        if blocking_canaries:
                            self.write_index()
                            raise RuntimeError(
                                "C1 Unity concurrency canary blocked paid recovery: "
                                + ", ".join(blocking_canaries)
                            )
                try:
                    if selected_formal_c1:
                        self._run_c1_latin_square(selected_formal_c1)
                finally:
                    if (
                        self.c1_sandbox_manager is not None
                        and not any(
                            arm in C1_DIAGNOSTIC_ARMS for arm in self.arms
                        )
                    ):
                        removed = self.c1_sandbox_manager.cleanup_stale()
                        if self.c1_sandbox_preflight is not None:
                            self.c1_sandbox_preflight[
                                "stale_sandboxes_removed_at_finish"
                            ] = removed
            for arm in self.arms:
                if arm in C1_ARMS:
                    continue
                runner = runners.get(arm)
                if runner is None:
                    continue
                runner()
            if (
                any(arm in C1_DIAGNOSTIC_ARMS for arm in self.arms)
                and self.c1_sandbox_manager is not None
            ):
                removed = self.c1_sandbox_manager.cleanup_stale()
                if self.c1_sandbox_preflight is not None:
                    self.c1_sandbox_preflight[
                        "stale_sandboxes_removed_at_finish"
                    ] = removed
        return self.write_index()

    def write_index(self) -> dict[str, Any]:
        with self._index_lock:
            return self._write_index_unlocked()


@contextlib.contextmanager
def _fake_visual_env():
    prev = os.environ.get("WARPTEST_VISUAL_JUDGE_FAKE")
    os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = "correct"
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop("WARPTEST_VISUAL_JUDGE_FAKE", None)
        else:
            os.environ["WARPTEST_VISUAL_JUDGE_FAKE"] = prev


@contextlib.contextmanager
def _runtime_lock_dir_env(
    path: Path,
    *,
    runtime_lock_policy: dict[str, Any] | None = None,
):
    """Use a machine-shared runtime lease directory for every P5 process.

    ``path`` remains the report-local audit location, but must not scope the
    lease itself: two independently launched P5 commands can otherwise mutate
    the same game checkout concurrently.  A distributed launcher may use an
    explicit environment setting only when it resolves to the manifest-registered
    shared filesystem namespace.
    """

    policy = dict(runtime_lock_policy or _runtime_lock_policy({}))
    if policy.get("scope") != DEFAULT_RUNTIME_LOCK_SCOPE:
        raise ValueError("runtime lock policy is not machine-shared")
    raw_lock_dir = policy.get("lock_dir")
    if not isinstance(raw_lock_dir, str) or not raw_lock_dir:
        raise ValueError("runtime lock policy is missing lock_dir")
    wait_timeout_s = validate_runtime_lock_wait_timeout_s(
        policy.get(
            "wait_timeout_s", DEFAULT_RUNTIME_LOCK_WAIT_TIMEOUT_S
        )
    )
    shared_dir = Path(raw_lock_dir).expanduser().resolve()
    previous = os.environ.get(RUNTIME_LOCK_DIR_ENV)
    previous_wait = os.environ.get(RUNTIME_LOCK_WAIT_TIMEOUT_ENV)
    if previous and Path(previous).expanduser().resolve() != shared_dir:
        raise RuntimeError(
            f"{RUNTIME_LOCK_DIR_ENV} does not match the manifest-registered "
            "runtime_lock.lock_dir"
        )
    if previous_wait is not None:
        try:
            previous_wait_value = validate_runtime_lock_wait_timeout_s(
                float(previous_wait)
            )
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"{RUNTIME_LOCK_WAIT_TIMEOUT_ENV} must be a positive finite number"
            ) from error
        if previous_wait_value != wait_timeout_s:
            raise RuntimeError(
                f"{RUNTIME_LOCK_WAIT_TIMEOUT_ENV} does not match the "
                "manifest-registered runtime_lock.wait_timeout_s"
            )
    # Keep an auditable pointer in the run directory without creating a
    # report-scoped lock namespace that would defeat cross-P5 exclusion.
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(
        path.parent / "runtime-locks.json",
        {
            "scope": DEFAULT_RUNTIME_LOCK_SCOPE,
            "lock_dir": str(shared_dir),
            "wait_timeout_s": wait_timeout_s,
            "task_clock_policy": "starts_after_resource_acquisition",
            "report_lock_dir": str(path.resolve()),
        },
    )
    os.environ[RUNTIME_LOCK_DIR_ENV] = str(shared_dir)
    os.environ[RUNTIME_LOCK_WAIT_TIMEOUT_ENV] = f"{wait_timeout_s:g}"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(RUNTIME_LOCK_DIR_ENV, None)
        else:
            os.environ[RUNTIME_LOCK_DIR_ENV] = previous
        if previous_wait is None:
            os.environ.pop(RUNTIME_LOCK_WAIT_TIMEOUT_ENV, None)
        else:
            os.environ[RUNTIME_LOCK_WAIT_TIMEOUT_ENV] = previous_wait


@contextlib.contextmanager
def _formal_c1_cache_env(path: Path):
    """Use run-local hidden artifacts and disable visual-verdict reuse for C1."""

    updates = {
        "WARPTEST_C1_CACHE_ROOT": str(path.resolve()),
        "WARPTEST_C1_DISABLE_VISUAL_CACHE": "1",
        # Formal accounting must see every provider attempt.  OpenAI SDK
        # transport retries are opaque, so all recovery happens in the explicit
        # metered worker/task/orchestrator loops instead.
        "WARPTEST_VISUAL_BRIDGE_MAX_RETRIES": "0",
        "WARPTEST_GUI_AGENT_MAX_RETRIES": "0",
        "WARPTEST_SYNTHESIS_MAX_RETRIES": "0",
        "WARPTEST_VISUAL_JUDGE_MAX_RETRIES": "0",
    }
    previous = {name: os.environ.get(name) for name in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def plan(
    manifest: dict[str, Any],
    *,
    models: list[str],
    seeds: int,
    arms: list[str],
    task_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Dry-run plan: how many producer invocations each arm will make."""

    per_arm: dict[str, int] = {}
    for arm in arms:
        if arm in C1_EXECUTION_ARMS:
            per_arm[arm] = seeds * len(models)
        elif arm in {"c2_memory_baseline", "c3_fuzz"}:
            per_arm[arm] = seeds  # run-level seeds win over the arm-level default
            if arm == "c2_memory_baseline":
                per_arm[arm] *= len(models)
        else:
            per_arm[arm] = len(models)
    task_count = len(task_ids) if task_ids else int(manifest.get("c1_task_count", 0) or 0)
    c1_invocations = sum(
        per_arm.get(arm, 0) for arm in arms if arm in C1_EXECUTION_ARMS
    )
    paired_start_tasks = (
        task_count
        if any(arm in {"c1_state", "c1_state_minimal_agent"} for arm in arms)
        else 0
    )
    diagnostic_cfg = (
        manifest.get("arms", {}).get("c1_state_minimal_agent", {})
        if isinstance(manifest.get("arms"), dict)
        else {}
    )
    preflight_routes: list[dict[str, str]] = []
    if any(arm in C1_EXECUTION_ARMS for arm in arms):
        preflight_routes.extend(
            {
                "name": f"decision:{str(model).split('@', 1)[0]}",
                "model": str(model).split("@", 1)[0],
                "modality": "vision",
            }
            for model in models
        )
        if (
            "c1_state_minimal_agent" in arms
            and isinstance(diagnostic_cfg, dict)
            and diagnostic_cfg.get("transition_action_source") == "source_agent_v2"
        ):
            preflight_routes.extend(
                {
                    "name": f"source_transition:{str(model).split('@', 1)[0]}",
                    "model": str(model).split("@", 1)[0],
                    "modality": "text",
                }
                for model in models
            )
        preflight_routes.extend(
            {
                "name": f"synthesis:{str(model).split('@', 1)[0]}",
                "model": str(model).split("@", 1)[0],
                "modality": "text",
            }
            for model in models
        )
        bridge = (
            manifest.get("visual_bridge")
            if isinstance(manifest.get("visual_bridge"), dict)
            else {}
        )
        observer = str(bridge.get("observer_model") or "claude-sonnet-5@relay")
        preflight_routes.append(
            {
                "name": "visual_observer",
                "model": observer.split("@", 1)[0],
                "modality": "vision",
            }
        )
        preflight_routes.extend(
            {
                "name": probe.name,
                "model": probe.model.split("@", 1)[0],
                "modality": "vision",
            }
            for probe in grounder_preflight_probes(
                models, observer=observer, configured=bridge.get("grounder_model")
            )
        )
        judge = (
            manifest.get("visual_judge")
            if isinstance(manifest.get("visual_judge"), dict)
            else {}
        )
        judge_model = str(
            judge.get("model")
            or manifest.get("visual_judge_model")
            or "qwen3-vl-flash@relay"
        )
        preflight_routes.append(
            {
                "name": "visual_judge",
                "model": judge_model.split("@", 1)[0],
                "modality": "vision",
            }
        )
    return {"models": models, "seeds": seeds, "arms": arms, "task_ids": task_ids,
            "provider_timeout_s": (
                manifest_provider_timeout_s(manifest)
                if any(arm in C1_EXECUTION_ARMS for arm in arms)
                else None
            ),
            "task_timeout_s": (
                validate_task_timeout_s(
                    manifest.get("c1_task_timeout_s", DEFAULT_C1_TASK_TIMEOUT_S)
                )
                if any(arm in C1_EXECUTION_ARMS for arm in arms)
                else None
            ),
            "c1_trial_persistence": _c1_trial_persistence_policy(manifest),
            "usage_accounting": _usage_accounting_policy(manifest),
            "c1_start_gate_protocol": _c1_start_gate_protocol(manifest),
            "tasks_per_c1_invocation": task_count or None,
            "paired_start_preparation_tasks": paired_start_tasks,
            "paired_start_preparation": (
                {
                    "protocol": "c1-paired-start-gate-v1",
                    "execution": "lazy_first_consumer_before_transition",
                    "scheduler": "inherits_c1_inner_worker_and_runtime_locks",
                    "trial_denominator_increment": 0,
                }
                if paired_start_tasks
                else None
            ),
            "source_transition_protocol": (
                {
                    "transition_action_source": diagnostic_cfg.get(
                        "transition_action_source"
                    ),
                    "max_steps": diagnostic_cfg.get("source_action_max_steps"),
                    "max_tokens": diagnostic_cfg.get("source_action_max_tokens"),
                    "format_repairs": diagnostic_cfg.get(
                        "source_action_format_repairs"
                    ),
                    "prompt_revision": diagnostic_cfg.get(
                        "source_action_prompt_revision"
                    ),
                    "pair_start_protocol": diagnostic_cfg.get(
                        "pair_start_protocol"
                    ),
                }
                if "c1_state_minimal_agent" in arms
                else None
            ),
            "preflight_route_count": len(preflight_routes),
            "preflight_routes": preflight_routes,
            "total_c1_trials": c1_invocations * task_count if task_count else None,
            "invocations_per_arm": per_arm,
            "total_invocations": sum(per_arm.values())}


def _route_models(manifest: dict[str, Any], models: list[str]) -> list[str]:
    api_cfg = manifest.get("api") if isinstance(manifest.get("api"), dict) else {}
    provider = str(api_cfg.get("provider") or "").strip()
    if not provider:
        return models
    return [model if "@" in model else f"{model}@{provider}" for model in models]


def _validate_c1_models(
    manifest: dict[str, Any], models: list[str], arms: list[str]
) -> None:
    if not any(arm in C1_EXECUTION_ARMS for arm in arms):
        return
    registered_raw = manifest.get("models")
    if not isinstance(registered_raw, list) or not registered_raw:
        raise ValueError("Formal C1 manifest must register its decision models")
    registered = set(_route_models(manifest, [str(model) for model in registered_raw]))
    unexpected = sorted(set(models) - registered)
    if unexpected:
        raise ValueError(
            "C1 --models may only select a subset of the pre-registered manifest models: "
            + ", ".join(unexpected)
        )


def _c1_tasks_path(manifest: dict[str, Any]) -> str:
    arms = manifest.get("arms", {}) if isinstance(manifest.get("arms"), dict) else {}
    for arm in (
        "c1_agent_s",
        "c1_agent_s_synth",
        "c1_state",
        "c1_state_minimal_agent",
        "c1_gui",
        "c1_gui_synth",
    ):
        cfg = arms.get(arm)
        if isinstance(cfg, dict) and cfg.get("tasks"):
            return str(cfg["tasks"])
    return "benchmarks/p5/c1_tasks.yaml"


def _validate_task_ids(base: Path, manifest: dict[str, Any], task_ids: list[str] | None) -> None:
    if not task_ids:
        return
    source = _c1_tasks_path(manifest)
    path = Path(source)
    if not path.is_absolute():
        path = base / path
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    available = {
        str(task.get("id"))
        for task in (data.get("tasks") or [])
        if isinstance(task, dict) and task.get("id")
    } if isinstance(data, dict) else set()
    missing = [task_id for task_id in task_ids if task_id not in available]
    if missing:
        raise ValueError("Unknown C1 task id(s): " + ", ".join(missing))


def _run_lazarus_scale(
    base: Path,
    manifest: dict[str, Any],
    *,
    models: list[str] | None,
    seeds: int | None,
    arms: list[str] | None,
    smoke: bool,
    dry_run: bool,
    task_ids: list[str] | None,
    c1_outer_worker: int,
    c1_inner_worker: int,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    """Dispatch the independent Lazarus family without changing C1 arm logic."""

    raw_source = manifest.get("tasks")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError("Lazarus manifest is missing its task source")
    source = Path(raw_source).expanduser()
    if not source.is_absolute():
        source = (base / source).resolve()
    else:
        source = source.resolve()
    if base != source and base not in source.parents:
        raise ValueError("Lazarus task source escapes repository root")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("task_schema") != "c1_lazarus_v1"
        or not isinstance(data.get("tasks"), list)
    ):
        raise ValueError("Lazarus task source must be a c1_lazarus_v1 catalog")
    if data.get("formal_population") is not True:
        raise ValueError("Lazarus task source must declare formal_population: true")
    raw_hosts = data.get("benchmark_hosts")
    if not isinstance(raw_hosts, dict) or tuple(raw_hosts) != LAZARUS_FORMAL_BENCHMARK_IDS:
        raise ValueError("Lazarus formal catalog benchmark_hosts drifted")
    task_benchmarks = {
        str(row.get("benchmark") or "")
        for row in data["tasks"]
        if isinstance(row, dict)
    }
    if task_benchmarks != set(LAZARUS_FORMAL_BENCHMARK_IDS):
        raise ValueError("Lazarus formal catalog task benchmarks drifted")
    catalog_ids = tuple(
        str(row.get("id") or "")
        for row in data["tasks"]
        if isinstance(row, dict)
    )
    catalog_rows = tuple(row for row in data["tasks"] if isinstance(row, dict))
    runtime_support = lazarus_runtime_support_report(catalog_rows)
    # A dry run is a planning tool and must stay usable while adapters are still
    # being delivered; it reports the coverage gap instead of hiding it.  Only a
    # dispatch is refused, and it is refused before any provider call.
    contract = validate_lazarus_manifest(
        manifest,
        catalog_task_ids=catalog_ids,
        unbound_task_ids=(
            () if dry_run else _lazarus_unbound_task_ids(base, catalog_rows)
        ),
    )
    selected_ids = tuple(task_ids) if task_ids else contract.task_ids
    if not selected_ids or len(selected_ids) != len(set(selected_ids)):
        raise ValueError("Lazarus task selection must be non-empty and unique")
    missing = sorted(set(selected_ids) - set(contract.task_ids))
    if missing:
        raise ValueError("Unknown Lazarus task id(s): " + ", ".join(missing))
    selected_arms = tuple(arms) if arms else contract.arms
    if selected_arms != contract.arms:
        raise ValueError("Lazarus v1 runs require both registered arms in order")
    selected_models = tuple(models) if models else (contract.model,)
    routed_models = tuple(
        model if "@" in model else f"{model}@relay" for model in selected_models
    )
    if routed_models != (contract.model,):
        raise ValueError(
            f"Lazarus v1 runs require the registered model {contract.model}"
        )
    seed_count = 1 if smoke and seeds is None else (seeds if seeds is not None else 3)
    if not isinstance(seed_count, int) or isinstance(seed_count, bool) or seed_count < 1:
        raise ValueError("Lazarus seed count must be a positive integer")
    seed_values = tuple(range(seed_count))
    if any(seed not in contract.seeds for seed in seed_values):
        raise ValueError("Lazarus seed selection exceeds registered seeds 0, 1, 2")
    formal = (
        selected_ids == contract.task_ids
        and seed_values == contract.seeds
        and not smoke
    )
    trial_plan = build_lazarus_trial_plan(
        selected_ids,
        seeds=seed_values,
        model=contract.model,
        formal=formal,
    )
    serialized_plan = {
        "complete": trial_plan.complete,
        "formal": trial_plan.formal,
        "failures": list(trial_plan.failures),
        "trial_count": trial_plan.trial_count,
        "task_ids": list(selected_ids),
        "arms": list(contract.arms),
        "versions": list(contract.versions),
        "models": [contract.model],
        "seeds": list(seed_values),
        "rotation_rule": trial_plan.rotation_rule,
    }
    if dry_run:
        return {
            "experiment_id": "c1_lazarus",
            "dry_run": True,
            "plan": serialized_plan,
            "runtime_support": runtime_support,
            "scheduler": {
                "c1_outer_worker": c1_outer_worker,
                "c1_inner_worker": c1_inner_worker,
                "maximum_task_concurrency": c1_outer_worker * c1_inner_worker,
                "ready_scheduler": True,
                "unity_canary": False,
            },
            "manifest_hash": run_context.manifest_hash(manifest),
        }
    if selected_ids != contract.task_ids or seed_values != contract.seeds:
        raise ValueError(
            "Lazarus paid execution runs the whole registered matrix; use "
            "--dry-run to inspect a subset"
        )
    # Dispatch stays outside the legacy C1 scheduler: this delegates to the
    # dedicated Lazarus orchestrator, which owns preflight, credential scope and
    # the group-serial schedule that checkpoint reuse depends on.
    from .lazarus_formal import launch_lazarus_formal

    run_dir = Path(report_dir) if report_dir is not None else new_report_dir() / "lazarus"
    launch = launch_lazarus_formal(
        base,
        report_path=run_dir / "formal-run.json",
        index_path=run_dir / "formal-run-index.json",
        outer_workers=c1_outer_worker,
    )
    return {
        "experiment_id": "c1_lazarus",
        "report_dir": str(run_dir),
        "report": str(launch.report_path),
        "run_index": str(launch.index_path),
        "complete": launch.run.complete,
        "stopped_early": launch.run.stopped_early,
        "stop_reason": launch.run.stop_reason,
        "observed_cells": len(launch.run.reports),
        "expected_cells": launch.preflight.plan.trial_count,
        "plan": serialized_plan,
        "runtime_support": runtime_support,
        "manifest_hash": run_context.manifest_hash(manifest),
    }


def _lazarus_unbound_task_ids(
    base: Path, tasks: Sequence[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Return every row a paid Lazarus dispatch cannot score.

    Two independent sources are consulted because they answer different
    questions.  ``unbound_formal_task_ids`` reads each oracle spec and reports
    rows whose offline probe has no binding; the runtime registry reports rows
    with no provider-backed runtime.  A row that fails either check would buy a
    cell that cannot produce a verdict.
    """

    spec_declared = unbound_formal_task_ids(tasks, repository_root=base)
    without_runtime = paid_runtime_unbound_task_ids(tasks)
    return tuple(dict.fromkeys((*spec_declared, *without_runtime)))


def _run_lazarus_phase0_scale(
    base: Path,
    manifest: dict[str, Any],
    *,
    models: list[str] | None,
    seeds: int | None,
    arms: list[str] | None,
    smoke: bool,
    dry_run: bool,
    task_ids: list[str] | None,
    c1_outer_worker: int,
    c1_inner_worker: int,
) -> dict[str, Any]:
    """Plan the explicitly non-formal eight-cell Lazarus pilot.

    The formal 24-task validation deliberately remains in
    :func:`_run_lazarus_scale`; this path cannot silently broaden into the
    formal matrix before the future catalog and review exist.
    """

    del smoke
    raw_source = manifest.get("tasks")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ValueError("Lazarus Phase 0 manifest is missing its task source")
    source = Path(raw_source).expanduser()
    source = (base / source).resolve() if not source.is_absolute() else source.resolve()
    if base != source and base not in source.parents:
        raise ValueError("Lazarus Phase 0 task source escapes repository root")
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or data.get("task_schema") != "c1_lazarus_v1"
        or not isinstance(data.get("tasks"), list)
    ):
        raise ValueError("Lazarus Phase 0 task source must be a c1_lazarus_v1 catalog")
    catalog_ids = tuple(
        str(row.get("id") or "") for row in data["tasks"] if isinstance(row, dict)
    )
    contract = validate_lazarus_phase0_manifest(
        manifest,
        catalog_task_ids=catalog_ids,
    )
    selected_ids = tuple(task_ids) if task_ids else contract.task_ids
    if selected_ids != contract.task_ids:
        raise ValueError("Lazarus Phase 0 must run both registered tasks in order")
    selected_arms = tuple(arms) if arms else contract.arms
    if selected_arms != contract.arms:
        raise ValueError("Lazarus Phase 0 requires both registered arms in order")
    selected_models = tuple(models) if models else (contract.model,)
    routed_models = tuple(
        model if "@" in model else f"{model}@relay" for model in selected_models
    )
    if routed_models != (contract.model,):
        raise ValueError(
            f"Lazarus Phase 0 requires the registered model {contract.model}"
        )
    if seeds not in {None, 1}:
        raise ValueError("Lazarus Phase 0 requires exactly seed 0")
    trial_plan = build_lazarus_trial_plan(
        contract.task_ids,
        seeds=(0,),
        model=contract.model,
        formal=False,
    )
    serialized_plan = {
        "complete": trial_plan.complete,
        "formal": False,
        "phase0_only": True,
        "failures": list(trial_plan.failures),
        "trial_count": trial_plan.trial_count,
        "task_ids": list(contract.task_ids),
        "arms": list(contract.arms),
        "versions": list(contract.versions),
        "models": [contract.model],
        "seeds": [0],
        "rotation_rule": trial_plan.rotation_rule,
    }
    if dry_run:
        return {
            "experiment_id": "c1_lazarus_phase0",
            "dry_run": True,
            "plan": serialized_plan,
            "scheduler": {
                "c1_outer_worker": c1_outer_worker,
                "c1_inner_worker": c1_inner_worker,
                "maximum_task_concurrency": 1,
                "ready_scheduler": True,
                "unity_canary": True,
                "serial": True,
            },
            "manifest_hash": run_context.manifest_hash(manifest),
        }
    raise RuntimeError(
        "Lazarus Phase 0 dispatch requires the dedicated preflight and runtime "
        "orchestrator; use dry-run until that gate is complete"
    )


def run_p5_scale(
    root: str | Path = Path.cwd(),
    *,
    manifest_path: str | Path = DEFAULT_MANIFEST,
    models: list[str] | None = None,
    seeds: int | None = None,
    arms: list[str] | None = None,
    smoke: bool = False,
    dry_run: bool = False,
    aggregate: bool = False,
    headless: bool = True,
    report_dir: Path | None = None,
    memory_workers: int = 1,
    c1_outer_worker: int = 8,
    c1_inner_worker: int = 4,
    resume: bool = False,
    preflight_artifact: Path | None = None,
    calibration_artifact: Path | None = None,
    task_ids: list[str] | None = None,
    c1_source_index: Path | None = None,
    c1_source_contract_migration: Path | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    manifest = load_manifest(manifest_path)
    if manifest.get("experiment_id") == "c1_lazarus":
        return _run_lazarus_scale(
            base,
            manifest,
            models=models,
            seeds=seeds,
            arms=arms,
            smoke=smoke,
            dry_run=dry_run,
            task_ids=task_ids,
            c1_outer_worker=c1_outer_worker,
            c1_inner_worker=c1_inner_worker,
            report_dir=report_dir,
        )
    if manifest.get("experiment_id") == "c1_lazarus_phase0":
        return _run_lazarus_phase0_scale(
            base,
            manifest,
            models=models,
            seeds=seeds,
            arms=arms,
            smoke=smoke,
            dry_run=dry_run,
            task_ids=task_ids,
            c1_outer_worker=c1_outer_worker,
            c1_inner_worker=c1_inner_worker,
        )
    arms = arms or list(DEFAULT_ARMS)
    unsupported_arms = [arm for arm in arms if arm not in ALL_ARMS]
    if unsupported_arms:
        raise ValueError(
            "normal P5 manifest cannot select non-C1/C2/C3 arm(s): "
            + ", ".join(dict.fromkeys(unsupported_arms))
        )
    agent_kind = "deterministic" if smoke else "llm"
    fake_visual = smoke
    resolved_models = models or (["deterministic"] if smoke else list(manifest.get("models", [])))
    if not smoke:
        resolved_models = _route_models(manifest, resolved_models)
        _validate_c1_models(manifest, resolved_models, arms)
    resolved_task_ids = _ordered_c1_task_ids(manifest, task_ids)
    _validate_task_ids(base, manifest, resolved_task_ids)
    resolved_seeds = seeds if seeds is not None else (1 if smoke else int(manifest.get("seeds", 1)))
    if resolved_seeds < 1:
        raise ValueError("seeds must be at least 1")
    if "c1_state" in arms:
        from .synthesis_ablation import validate_variant
        variant = validate_variant(str(manifest.get("arms", {}).get("c1_state", {}).get("synthesis_ablation", "full")))
        if variant != "full" or manifest.get("synthesis_ablation_study"):
            if arms != ["c1_state"] or resolved_seeds != 1 or int(manifest.get("seed_base", 0)) != 0:
                raise ValueError("The synthesis ablation study requires only c1_state and seed 0")
    if memory_workers < 1:
        raise ValueError("memory_workers must be at least 1")
    if c1_outer_worker < 1:
        raise ValueError("c1_outer_worker must be at least 1")
    if c1_inner_worker < 1:
        raise ValueError("c1_inner_worker must be at least 1")

    if dry_run:
        return {"dry_run": True, "plan": plan(
                    manifest, models=resolved_models, seeds=resolved_seeds, arms=arms, task_ids=resolved_task_ids
                ),
                "scheduler": {
                    "c1_outer_worker": c1_outer_worker,
                    "c1_inner_worker": c1_inner_worker,
                    "maximum_task_concurrency": c1_outer_worker * c1_inner_worker,
                },
                "manifest_hash": run_context.manifest_hash(manifest)}

    cost_value = cost_env_value(manifest)
    if cost_value and not os.environ.get("WARPTEST_LLM_USD_PER_MTOK_BY_MODEL"):
        os.environ["WARPTEST_LLM_USD_PER_MTOK_BY_MODEL"] = cost_value

    run_dir = report_dir or (new_report_dir() / "p5")
    run_dir.mkdir(parents=True, exist_ok=True)
    api_cfg = manifest.get("api") if isinstance(manifest.get("api"), dict) else {}
    credentials_file = api_cfg.get("credentials_file")
    credentials_cm = contextlib.nullcontext()
    if (
        not smoke
        and any(
            arm in C1_EXECUTION_ARMS or arm == "c2_memory_baseline"
            for arm in arms
        )
        and isinstance(credentials_file, str)
    ):
        credential_path = Path(credentials_file)
        if not credential_path.is_absolute():
            credential_path = base / credential_path
        credentials_cm = relay_environment(credential_path)
    with credentials_cm:
        orchestrator = P5Orchestrator(
            base, manifest, run_dir=run_dir, models=resolved_models, seeds=resolved_seeds,
            arms=arms, agent_kind=agent_kind, headless=headless, fake_visual=fake_visual,
            memory_workers=memory_workers, resume=resume,
            c1_outer_worker=c1_outer_worker,
            c1_inner_worker=c1_inner_worker,
            preflight_artifact=preflight_artifact,
            calibration_artifact=calibration_artifact, task_ids=resolved_task_ids,
            c1_source_index=c1_source_index,
            c1_source_contract_migration=c1_source_contract_migration,
        )
        index = orchestrator.run()
    result: dict[str, Any] = {"report_dir": str(run_dir), "run_index": str(run_dir / "p5-run-index.json"),
                              "index": index}
    if aggregate:
        stats_cfg = manifest.get("stats", {})
        summary = p5_aggregate.aggregate(
            index, manifest=manifest, report_root=base,
            rng_seed=int(stats_cfg.get("rng_seed", 1234)),
            iters=int(stats_cfg.get("bootstrap_iterations", 10000)),
            ci=float(stats_cfg.get("ci", 0.95)),
        )
        write_json(run_dir / "p5-scale-summary.json", summary)
        (run_dir / "p5-scale-summary.md").write_text(p5_aggregate.render_markdown(summary), encoding="utf-8")
        result["summary"] = str(run_dir / "p5-scale-summary.json")
    return result
