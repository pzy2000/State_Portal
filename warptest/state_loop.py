"""State Loop MVP: a state-management driven Gen+Test loop for long-horizon GUI software.

Task format (new):
  Input  : a state/test-target description (text) + a repo.
  Output : a checkpoint config (the method's ``generated_spec``) -- config only.
  Eval   : run -> GUI screenshot_before / screenshot_after -> VS. oracle goldens -> True/False.

Per task the loop:
  1. asks the synthesis agent for a config (``generated_spec``) and writes it as a
     versioned config file (``vN/spec.yaml``);
  2. auto-generates oracle before/after goldens by running the hidden ``oracle_spec``
     once and cross-validating the capture (Repo_Oracle -> goldens);
  3. reaches the candidate before/after states on a warm :class:`StateSession`,
     reusing the dev server/browser across iterations (state management), and records
     cold-vs-warm restore timing;
  4. judges the before/after screenshot pair against the goldens; the task passes iff
     both halves match.

State management is the headline: the test half of each loop iteration runs on a
persistent runtime with a deep-state snapshot cache, so it does not re-pay server /
browser startup or full replay every time.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
import copy
import functools
import hashlib
import json
import os
import shutil
import subprocess
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import yaml

from . import cost, run_context
from .c1_evaluation import evaluation_contract, semantic_assertions, visual_oracle
from .c1_concurrency import (
    C1GlobalStop,
    C1ReadyHandle,
    C1ReadyInvocationLifecycle,
    C1ReadyScheduler,
    C1ReadyTicket,
    C1TaskDeferred,
    C1TaskDispatchCancelled,
    build_lane_plan,
    build_ready_task_plan,
    retry_ready_invocation_id,
)
from .c1_budgets import c1_task_budget_ms
from .c1_catalog import validate_c1_v5_catalog
from .c1_task_deadline import (
    C1TaskTimeout,
    DEFAULT_C1_TASK_TIMEOUT_S,
    c1_task_deadline,
    raise_if_task_timed_out,
    validate_task_timeout_s,
)
from .c1_task_resources import (
    canonical_unity_serialized_benchmarks,
    c1_task_logical_resources,
    c1_task_resource_locks,
)
from .c1_sandbox import C1SandboxManager
from .c1_golden_artifacts import capture_oracle_golden_artifact_digests
from .c1_state_metrics import (
    STATE_STAGE_METRICS_SCHEMA_VERSION,
    initialize_state_stage_metrics,
)
from .c1_desktop_runtime import (
    BLENDER_SEMANTIC_SOURCE,
    C1DesktopRuntimeError,
    C1DesktopRuntimeUnsupported,
    LIBREOFFICE_SEMANTIC_SOURCE,
    BlenderC1DesktopSession,
    LibreOfficeC1DesktopSession,
    semantic_probe_passed,
)
from .c1_browser import c1_task_headless
from .blender_runtime import find_blender_executable
from .c1_web_runtime import create_c1_web_session
from .c1_openra_runtime import (
    OPENRA_DEFAULT_SETTINGS,
    OpenRAGameplaySession,
    OpenRASettingsSession,
    apply_setting_actions as apply_openra_setting_actions,
    target_settings as openra_target_settings,
)
from .c1_start_semantics import (
    UNSUPPORTED_STATUS as ORACLE_TARGET_UNSUPPORTED,
    merge_start_semantic,
    oracle_target,
    semantic_start_grade,
)
from .c1_synthesis_cache import (
    C1_SYNTHESIS_CONSUMER,
    C1_SYNTHESIS_EXPLORE,
    C1_SYNTHESIS_MAX_TOKENS,
    SELF_SYNTHESIS_CACHE_SCHEMA_VERSION,
    atomic_write_json,
    cache_identity_root,
    canonical_hash,
    obtain_synthesis,
    synthesis_cache_key,
    synthesis_outcome,
    synthesis_outcome_usable,
)
from .c1_runtime_hold import (
    NO_HOLD_BACKEND,
    TERMINATED_BACKEND,
    hold_runtime_state_for_start_grading,
)
from .c1_spec_acceptance import (
    UNITY_SPEC_CONTRACTS,
    accept_c1_generated_spec,
    canonical_c1_benchmark,
    checkpoint_payload,
    generated_spec_acceptance_errors,
    seed_generated_spec,
)
from .c1_minimal_smoke_agent import (
    MINIMAL_SMOKE_AGENT_FORMAT_REPAIRS,
    MINIMAL_SMOKE_AGENT_MAX_ACTIONS,
    MINIMAL_SMOKE_AGENT_MAX_TOKENS,
    MinimalSmokeActionAgent,
)
from .c1_source_transition_agent import (
    SOURCE_TRANSITION_AGENT_FORMAT_REPAIRS,
    SOURCE_TRANSITION_AGENT_MAX_STEPS,
    SOURCE_TRANSITION_AGENT_MAX_TOKENS,
    SourceTransitionActionAgent,
)
from .model_request import model_request_profile_provenance
from .discovery import RepositoryInspector
from .jynew_runtime import JynewRuntimeAdapter
from .luanti_state import LuantiWarmSession, luanti_daylight_mode_for_task
from .luanti_runtime import luanti_client_source_identity
# Compatibility export; formal background paths do not call it.
from .native_input import native_runtime_lock as native_runtime_lock
from .pal3_runtime import Pal3RuntimeAdapter
from .profiles import profile_for
from .reports import new_report_dir
from .runner import load_spec
from .runtime import BrowserRuntimeAdapter
from .runtime_lock import (
    RuntimeLockWaitTimeout,
    RuntimeResourceBusy,
    exclusive_file_lease,
    runtime_lock_wait_timeout_s,
    runtime_lease,
    runtime_lock_wait_ledger,
    runtime_lock_wait_payload,
)
from .spec_schema import validate_spec_schema
from .state_store import (
    StateManagementUnsupported,
    StateSession,
    StateStore,
    measure_cold_restore,
    state_fingerprint,
)
from .providers import resolve_model
from .synthesis import DEFAULT_SYNTHESIS_MODEL, SynthesisError, build_synthesis_agent
from .synthesis_ablation import ABLATION_REVISION, ablation_scope, validate_variant
from .task_schema import MethodCheckpoint, PublicIssue, resolve_task_repo
from .task_descriptions import canonical_state_description
from .visual import run_visual_pair_audit, screenshot_quality

STATE_LOOP_TASKS = Path("benchmarks/state_loop/tasks.yaml")
STATE_TRANSITION_ACTION_SOURCES = {
    "generated_spec",
    "minimal_agent_v1",
    "source_agent_v2",
}
PLANNED_TRANSITION_ACTION_SOURCES = frozenset(
    {"minimal_agent_v1", "source_agent_v2"}
)
MINIMAL_STATE_ARM = "c1_state_minimal_agent"
# C3 intentionally uses a separate, handler-backed task universe.  In
# particular it contains OpenRA's native gameplay fuzz tasks but does not alter
# the C1 State Loop task set above.
STATE_FUZZ_TASKS = Path("benchmarks/state_loop/tasks_c3.yaml")
GOLDEN_CACHE_ROOT = Path(".warptest-cache") / "state-loop-golden"
REFERENCE_REPLAY_SCHEMA_VERSION = 2
C1_CACHE_ROOT_ENV = "WARPTEST_C1_CACHE_ROOT"
ORACLE_GOLDEN_CACHE_SCHEMA_VERSION = 2
BLENDER_STATE_TRANSITION_BACKEND = "blender_live_bpy_state_transition_v1"
LIBREOFFICE_STATE_TRANSITION_BACKEND = "uno_mutation_native_ui_save_reopen_v1"
OPENRA_STATE_PAIR_SOURCE = "openra_state_process_pair"
OPENRA_STATE_SNAPSHOT_SOURCE = "openra_state_live_process"
OPENRA_STATE_PAIR_ALGORITHM = "not_applicable_public_settings_pair"
OPENRA_GAMEPLAY_STATE_PAIR_SOURCE = "openra_gameplay_state_live_pair"
OPENRA_GAMEPLAY_STATE_PAIR_ALGORITHM = "live_registered_gameplay_transition_v1"
UNITY_STATE_PAIR_SOURCE = "unity_state_checkpoint_process_pair"
UNITY_STATE_SNAPSHOT_SOURCE = "unity_state_checkpoint_process"
UNITY_STATE_PAIR_ALGORITHM = "not_applicable_unity_checkpoint_pair"
UNITY_STATE_EVIDENCE_PROTOCOL = "warptest-unity-checkpoint-state-v1"
LIBREOFFICE_STATE_PERSISTENCE_CHECKS = frozenset(
    {
        "transition.session_same",
        "transition.saved_url",
        "transition.persisted_file",
        "transition.not_modified",
        "transition.document_reopened",
    }
)

# Moved to c1_spec_acceptance so the GUI arm enforces the same Unity
# executability contract; re-exported here for existing importers.
_UNITY_STATE_SPEC_CONTRACTS = UNITY_SPEC_CONTRACTS


def _c1_cache_root(default: str | Path) -> Path:
    override = os.environ.get(C1_CACHE_ROOT_ENV)
    return Path(override or default).expanduser().resolve()


REPLAY_PROGRAMMATIC_START_GATE_ENV = "WARPTEST_REPLAY_PROGRAMMATIC_START_GATE"


def replay_programmatic_start_gate() -> bool:
    """True when the Start gate must be decided by the programmatic channel.

    Only the replay-determinism experiment sets this. Formal C1 always keeps
    the conjunction, because there the visual channel is graded against a
    rubric and is meant to be able to reject a Start.
    """

    return os.environ.get(REPLAY_PROGRAMMATIC_START_GATE_ENV) == "1"


@dataclass
class StateLoopTask:
    issue: PublicIssue
    oracle_spec: str
    state_description: str
    preconditions: str = ""
    reference_steps: str = ""
    goal_description: str = ""
    # Descriptive label only. It once selected the step and wall-clock budgets;
    # both are now uniform, so it influences no execution parameter and must not
    # be used as a difficulty axis in reporting.
    horizon_class: str = "short"
    prompt_revision: str = ""
    # Grader-side analysis metadata. This is intentionally separate from
    # PublicIssue.tags so capability labels never enter the model-visible task
    # payload.
    capabilities: tuple[str, ...] = ()
    # Trusted coordinator-only extensions.  PublicIssue remains the sole model
    # payload, so version construction and hidden defect data never cross that
    # boundary.
    coordinator_metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def id(self) -> str:
        return self.issue.id

    @property
    def benchmark(self) -> str:
        return self.issue.benchmark


def audit_scripted_reference_replay(
    root: str | Path,
    task: StateLoopTask,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    *,
    seed: int,
    headless: bool,
    cache_root: str | Path | None = None,
) -> dict[str, Any]:
    """Execute the task's declared clean-start replay and cache auditable evidence.

    ``reference_reachable`` is a property of the scripted replay, never of oracle
    checkpoint injection.  A profile that deliberately excludes replay, or a spec
    with no ``replay_from_start`` plan, is a valid ``False`` observation. Runtime
    failures are different: they leave reachability unknown (``None``) so callers
    can fail closed instead of silently classifying infrastructure noise.

    The cache is keyed by the seeded oracle bytes, relevant repo commit, profile,
    headed mode, and audit schema. It therefore shares one real replay across arms
    and models without re-running the hidden reference 1,200 times.
    """

    base = Path(root).expanduser().resolve()
    repo = Path(repo).expanduser().resolve()
    replay_declared = isinstance(oracle_spec.get("replay_from_start"), dict)
    default_cache_root = cache_root if cache_root is not None else base / ".warptest-cache"
    cache_base = _c1_cache_root(default_cache_root) / "c1-reference-replay"
    try:
        inspect_result = RepositoryInspector().inspect(repo)
        profile = profile_for(inspect_result)
    except Exception as error:
        fingerprint = hashlib.sha256(
            yaml.safe_dump(
                {
                    "schema_version": REFERENCE_REPLAY_SCHEMA_VERSION,
                    "task_id": task.id,
                    "benchmark": task.benchmark,
                    "seed": int(seed),
                    "headless": bool(headless),
                    "profile": None,
                    "repo_commit": None,
                    "oracle_spec": oracle_spec,
                },
                sort_keys=True,
                allow_unicode=False,
            ).encode("utf-8")
        ).hexdigest()[:24]
        evidence_path = cache_base / fingerprint / "audit.json"
        evidence = {
            "schema_version": REFERENCE_REPLAY_SCHEMA_VERSION,
            "task_id": task.id,
            "benchmark": task.benchmark,
            "seed": int(seed),
            "headless": bool(headless),
            "spec_fingerprint": fingerprint,
            "replay_declared": replay_declared,
            "status": "error",
            "reference_reachable": None,
            "reason": (
                "reference replay repository inspection failed: "
                f"{type(error).__name__}: {error}"
            ),
            "cache_hit": False,
            "evidence_file": str(evidence_path),
        }
        _write_reference_replay_evidence(evidence_path, evidence)
        return evidence
    fingerprint_payload = {
        "schema_version": REFERENCE_REPLAY_SCHEMA_VERSION,
        "task_id": task.id,
        "benchmark": task.benchmark,
        "seed": int(seed),
        "headless": bool(headless),
        "profile": inspect_result.profile,
        "repo_commit": inspect_result.metadata.git_commit,
        "oracle_spec": oracle_spec,
    }
    fingerprint = hashlib.sha256(
        yaml.safe_dump(
            fingerprint_payload,
            sort_keys=True,
            allow_unicode=False,
        ).encode("utf-8")
    ).hexdigest()[:24]
    evidence_dir = cache_base / fingerprint
    evidence_path = evidence_dir / "audit.json"

    def _cached() -> dict[str, Any] | None:
        evidence = _read_reference_replay_evidence(
            evidence_path, fingerprint=fingerprint
        )
        if evidence is None:
            return None
        return {**evidence, "cache_hit": True, "evidence_file": str(evidence_path)}

    def _produce() -> dict[str, Any]:
        common: dict[str, Any] = {
            "schema_version": REFERENCE_REPLAY_SCHEMA_VERSION,
            "task_id": task.id,
            "benchmark": task.benchmark,
            "seed": int(seed),
            "headless": bool(headless),
            "profile": inspect_result.profile,
            "repo_commit": inspect_result.metadata.git_commit,
            "spec_fingerprint": fingerprint,
            "replay_declared": replay_declared,
            "cache_hit": False,
            "evidence_file": str(evidence_path),
        }
        if not replay_declared:
            evidence = {
                **common,
                "status": "unsupported",
                "reference_reachable": False,
                "reason": "Spec does not declare replay_from_start as a scripted replay mapping.",
                "strategy_id": "deterministic_replay_from_start",
                "strategy_validity": "unknown",
                "target_validated_before_smoke": False,
            }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence
        try:
            plan = profile.strategy_plan(oracle_spec, "replay_from_start")
        except Exception as error:
            evidence = {
                **common,
                "status": "error",
                "reference_reachable": None,
                "reason": f"reference replay planning failed: {type(error).__name__}: {error}",
            }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence

        common.update(
            {
                "strategy_id": plan.strategy_id,
                "strategy_validity": plan.validity,
            }
        )
        if plan.unsupported_reason:
            evidence = {
                **common,
                "status": "unsupported",
                "reference_reachable": False,
                "reason": str(plan.unsupported_reason),
                "target_validated_before_smoke": False,
            }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence

        if (
            inspect_result.profile == "OpenRAProfile"
            and (oracle_spec.get("target") or {}).get("kind") == "openra_settings"
        ):
            try:
                expected = openra_target_settings(oracle_spec)
                non_default = []
                for section, values in expected.items():
                    for key, value in values.items():
                        if key == "IntroductionPromptVersion":
                            continue
                        if OPENRA_DEFAULT_SETTINGS.get(section, {}).get(key) != value:
                            non_default.append(f"{section}.{key}")
                if non_default:
                    raise RuntimeError(
                        "from-start OpenRA settings target is not an engine default: "
                        + ", ".join(non_default)
                    )
                evidence_dir.mkdir(parents=True, exist_ok=True)
                infrastructure_settings = {
                    "Game": {
                        "IntroductionPromptVersion": int(
                            expected.get("Game", {}).get(
                                "IntroductionPromptVersion", 999
                            )
                        )
                    }
                }
                with runtime_lease(repo):
                    with OpenRASettingsSession(
                        repo,
                        evidence_dir / "runtime",
                        settings=infrastructure_settings,
                        experiment_seed=int(seed),
                    ) as runtime:
                        grade = runtime.grade_settings(expected)
                        screenshot = runtime.screenshot(
                            evidence_dir / "reference-start.png"
                        )
                evidence = {
                    **common,
                    "status": "success" if grade.ok else "failure",
                    "reference_reachable": bool(grade.ok),
                    "reason": (
                        "clean OpenRA shell exposed the registered public default setting"
                        if grade.ok
                        else "clean OpenRA shell did not expose the registered public default setting"
                    ),
                    "target_validated_before_smoke": bool(grade.ok),
                    "checkpoint_restoration_status": "success" if grade.ok else "failure",
                    "validation_status": grade.status,
                    "overall_status": "success" if grade.ok else "failure",
                    "runtime_access_methods": {
                        "semantic": "isolated_public_settings_yaml",
                        "visual": "foreground_openra_window",
                    },
                    "hook_availability": {"private_gameplay_probe_used": False},
                    "reference_spec_mode": "clean_public_default_start",
                    "reference_start_screenshot": screenshot,
                    "reference_start_fingerprint": _image_fingerprint(screenshot),
                    "semantic_detail": grade.detail,
                    "metrics": {},
                }
            except Exception as error:
                evidence = {
                    **common,
                    "status": "error",
                    "reference_reachable": None,
                    "reason": f"OpenRA settings reference replay failed: {type(error).__name__}: {error}",
                }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence

        if (
            inspect_result.profile == "OpenRAProfile"
            and (oracle_spec.get("target") or {}).get("kind") == "openra_map"
        ):
            try:
                evidence_dir.mkdir(parents=True, exist_ok=True)
                reference_spec = copy.deepcopy(oracle_spec)
                reference_spec["actions"] = []
                reference_spec["assertions"] = []
                with runtime_lease(repo):
                    with OpenRAGameplaySession(
                        repo,
                        evidence_dir / "runtime",
                        spec=reference_spec,
                        experiment_seed=int(seed),
                    ) as runtime:
                        grade = runtime.grade_target()
                        screenshot = runtime.screenshot(
                            evidence_dir / "reference-start.png"
                        )
                evidence = {
                    **common,
                    "status": "success" if grade.ok else "failure",
                    "reference_reachable": bool(grade.ok),
                    "reason": (
                        "clean OpenRA gameplay launch exposed the registered start actors"
                        if grade.ok
                        else "clean OpenRA gameplay launch did not expose the registered start actors"
                    ),
                    "target_validated_before_smoke": bool(grade.ok),
                    "checkpoint_restoration_status": (
                        "success" if grade.ok else "failure"
                    ),
                    "validation_status": grade.status,
                    "overall_status": "success" if grade.ok else "failure",
                    "runtime_access_methods": {
                        "semantic": "openra_live_gameplay_probe_read_only_v1",
                        "visual": "openra_renderer_save_screenshot_sync_v1",
                    },
                    "hook_availability": {
                        "private_gameplay_probe_used": False,
                        "public_test_gated_gameplay_probe_used": True,
                    },
                    "reference_spec_mode": "clean_public_gameplay_start",
                    "reference_start_screenshot": screenshot,
                    "reference_start_fingerprint": _image_fingerprint(screenshot),
                    "semantic_detail": grade.detail,
                    "metrics": {
                        "world_tick": grade.detail.get("world_tick")
                        if isinstance(grade.detail, dict)
                        else None
                    },
                }
            except Exception as error:
                evidence = {
                    **common,
                    "status": "error",
                    "reference_reachable": None,
                    "reason": (
                        "OpenRA gameplay reference replay failed: "
                        f"{type(error).__name__}: {error}"
                    ),
                }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence

        # Every currently supported C1 scripted replay is browser-backed. Refuse to
        # guess an engine adapter if a future profile advertises replay without wiring
        # it here; that is an incomplete audit, not evidence of unreachability.
        if inspect_result.profile not in {
            "Game2048Profile",
            "DatingSimProfile",
            "SpaceRPGProfile",
        }:
            evidence = {
                **common,
                "status": "error",
                "reference_reachable": None,
                "reason": (
                    f"profile {inspect_result.profile} advertises replay support but "
                    "has no C1 reference runtime adapter"
                ),
            }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence

        try:
            evidence_dir.mkdir(parents=True, exist_ok=True)
            # The reachability audit stops at the declared checkpoint/start state. The
            # task's post-start transition and hidden goal assertions belong to grading,
            # not to the clean-start reference claim.
            reference_spec = copy.deepcopy(oracle_spec)
            reference_spec["actions"] = []
            reference_spec["assertions"] = []
            reference_screenshot: str | None = None
            with runtime_lease(repo):
                with BrowserRuntimeAdapter(
                    repo,
                    inspect_result.metadata.launch_command,
                    evidence_dir / "runtime",
                    headless=headless,
                    experiment_seed=int(seed),
                ) as runtime:
                    report = profile.run(
                        reference_spec,
                        oracle_spec_path,
                        runtime,
                        mode="replay_from_start",
                    )
                    try:
                        candidate = runtime.screenshot("reference-start.png")
                        if candidate and Path(candidate).exists():
                            reference_screenshot = str(Path(candidate).resolve())
                    except Exception:
                        reference_screenshot = None
            restoration_status = getattr(
                getattr(report, "checkpoint_restoration", None), "status", None
            )
            validation_status = getattr(
                getattr(report, "validation", None), "status", None
            )
            reached = bool(
                getattr(report, "target_validated_before_smoke", False)
                and restoration_status == "success"
                and validation_status == "success"
            )
            metrics = getattr(report, "metrics", {})
            if not isinstance(metrics, dict):
                metrics = {}
            reason = "scripted replay reached and validated the start state" if reached else str(
                getattr(getattr(report, "validation", None), "detail", "")
                or getattr(getattr(report, "checkpoint_restoration", None), "detail", "")
                or "scripted replay did not validate the start state"
            )
            evidence = {
                **common,
                "status": "success" if reached else "failure",
                "reference_reachable": reached,
                "reason": reason,
                "target_validated_before_smoke": bool(
                    getattr(report, "target_validated_before_smoke", False)
                ),
                "checkpoint_restoration_status": restoration_status,
                "validation_status": validation_status,
                "overall_status": getattr(report, "overall_status", None),
                "runtime_access_methods": getattr(report, "runtime_access_methods", {}),
                "hook_availability": getattr(report, "hook_availability", {}),
                "reference_spec_mode": "start_only",
                "reference_start_screenshot": reference_screenshot,
                "reference_start_fingerprint": _image_fingerprint(reference_screenshot),
                "metrics": {
                    key: metrics.get(key)
                    for key in (
                        "time_to_target_ms",
                        "time_to_first_action_ms",
                        "actions_replayed",
                    )
                    if metrics.get(key) is not None
                },
            }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence
        except Exception as error:
            evidence = {
                **common,
                "status": "error",
                "reference_reachable": None,
                "reason": f"scripted reference replay failed: {type(error).__name__}: {error}",
            }
            _write_reference_replay_evidence(evidence_path, evidence)
            return evidence

    hit = _cached()
    if hit is not None:
        return hit
    # One replay per fingerprint, even when several arms miss together. The
    # replay writes engine IPC handshake files inside ``evidence_dir``, and the
    # evidence is deliberately shared across arms and models, so two arms that
    # miss at the same moment would launch the same reference replay into one
    # directory and reject each other's session nonce. Each arm also runs in its
    # own sandbox clone, so the per-checkout lease cannot serialise them.
    with exclusive_file_lease(
        cache_base / "locks" / f"{fingerprint}.lock",
        resource_kind="c1-reference-replay",
    ):
        hit = _cached()
        if hit is not None:
            return hit
        return _produce()


def _read_reference_replay_evidence(
    path: Path, *, fingerprint: str
) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(value, dict):
        return None
    if value.get("spec_fingerprint") != fingerprint:
        return None
    if value.get("status") not in {"success", "failure", "unsupported"}:
        return None
    if not isinstance(value.get("reference_reachable"), bool):
        return None
    return value


def _write_reference_replay_evidence(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_c1_hidden_contract(
    base: Path,
    task_path: Path,
    task_id: Any,
    oracle_spec: str,
    *,
    lazarus: bool = False,
) -> None:
    """Fail closed when a formal task lacks either hidden grading contract."""

    spec_path = (base / oracle_spec).resolve()
    try:
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} cannot load oracle_spec "
            f"{oracle_spec}: {error}"
        ) from error
    if not isinstance(spec, dict):
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} has invalid oracle_spec"
        )
    from .c1_evaluation import evaluation_contract, validate_evaluation_contract

    if evaluation_contract(spec) is None:
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} requires evaluation-v1"
        )
    contract_errors = validate_evaluation_contract(
        spec, spec_path, allow_lazarus=lazarus
    )
    if contract_errors:
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} has invalid evaluation-v1: "
            + "; ".join(contract_errors)
        )
    if lazarus:
        from .lazarus_catalog import materialize_lazarus_spec

        views = [
            materialize_lazarus_spec(spec, view="goal"),
            materialize_lazarus_spec(spec, view="defect"),
        ]
    else:
        views = [spec]
    assertions = semantic_assertions(views[0])
    if not isinstance(assertions, list) or not assertions:
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} requires hidden assertions"
        )
    visual = visual_oracle(views[0])
    if not isinstance(visual, dict):
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} requires visual_oracle"
        )
    after_description = visual.get("after_description") or visual.get("description")
    after_rubric = visual.get("after_rubric") or visual.get("rubric")
    if not isinstance(after_description, str) or not after_description.strip():
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} requires a visual "
            "after-state description"
        )
    if not isinstance(after_rubric, dict) or not after_rubric:
        raise ValueError(
            f"{task_path}: formal C1 task {task_id} requires a visual "
            "after-state rubric"
        )
    if lazarus:
        defect_assertions = semantic_assertions(views[1])
        defect_visual = visual_oracle(views[1])
        if not isinstance(defect_assertions, list) or not defect_assertions:
            raise ValueError(
                f"{task_path}: formal C1 task {task_id} requires defect assertions"
            )
        if not isinstance(defect_visual, dict) or not defect_visual.get("golden_after"):
            raise ValueError(
                f"{task_path}: formal C1 task {task_id} requires buggy after golden"
            )


_C1_ANALYSIS_SCHEMA_VERSION = 1
_C1_BENCHMARK_TAGS = {
    "2048": "2048",
    "DatingSim": "love",
    "datingsim": "love",
    "SpaceRPG": "spacerpg",
    "spacerpg": "spacerpg",
    "luanti": "luanti",
    "OpenRA": "openra",
    "openra": "openra",
    "JupyterLab": "jupyterlab",
    "jupyterlab": "jupyterlab",
    "LibreOffice": "libreoffice",
    "libreoffice": "libreoffice",
    "Blender": "blender",
    "blender": "blender",
    "jynew": "jynew",
    "pal3": "pal3",
}
_C1_TAG_VOCABULARY = {
    "c1",
    "deep-chain",
    *_C1_BENCHMARK_TAGS.values(),
}
_C1_CAPABILITY_VOCABULARY = (
    "multi-step-planning",
    "gui-localization",
    "state-persistence",
    "numeric-reasoning",
    "spatial-reasoning",
    "resource-management",
    "structured-artifact-editing",
)


def _validate_c1_analysis_metadata(
    data: dict[str, Any], path: Path
) -> dict[str, tuple[str, ...]]:
    if data.get("analysis_schema_version") != _C1_ANALYSIS_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: formal C1 analysis_schema_version must be "
            f"{_C1_ANALYSIS_SCHEMA_VERSION}"
        )
    tag_vocabulary = data.get("tag_vocabulary")
    if (
        not isinstance(tag_vocabulary, list)
        or len(tag_vocabulary) != len(set(map(str, tag_vocabulary)))
        or set(map(str, tag_vocabulary)) != _C1_TAG_VOCABULARY
    ):
        raise ValueError(f"{path}: formal C1 tag_vocabulary is not the closed vocabulary")
    capability_vocabulary = data.get("capability_vocabulary")
    if (
        not isinstance(capability_vocabulary, dict)
        or tuple(capability_vocabulary) != _C1_CAPABILITY_VOCABULARY
    ):
        raise ValueError(
            f"{path}: formal C1 capability_vocabulary must contain the seven "
            "registered capabilities in canonical order"
        )
    for capability, policy in capability_vocabulary.items():
        if (
            not isinstance(policy, dict)
            or not isinstance(policy.get("description"), str)
            or not policy["description"].strip()
            or policy.get("minimum_tasks") != 3
            or policy.get("minimum_benchmarks") != 2
        ):
            raise ValueError(
                f"{path}: capability {capability} must declare a description, "
                "minimum_tasks=3, and minimum_benchmarks=2"
            )
    expansion_sets = data.get("expansion_sets")
    deep30 = expansion_sets.get("deep30") if isinstance(expansion_sets, dict) else None
    deep_ids_raw = deep30.get("task_ids") if isinstance(deep30, dict) else None
    if (
        not isinstance(deep_ids_raw, list)
        or len(deep_ids_raw) != 30
        or len(set(map(str, deep_ids_raw))) != 30
    ):
        raise ValueError(f"{path}: expansion_sets.deep30 must contain 30 unique task ids")
    deep_ids = set(map(str, deep_ids_raw))

    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        raise ValueError(f"{path}: formal C1 task list is missing")
    task_ids = {
        str(item.get("id"))
        for item in raw_tasks
        if isinstance(item, dict) and item.get("id")
    }
    if len(task_ids) != len(raw_tasks):
        raise ValueError(f"{path}: formal C1 task ids must be present and unique")
    selected_task_ids = data.get("selected_task_ids")
    derived_subset = selected_task_ids is not None
    if derived_subset:
        ordered_task_ids = [
            str(item["id"]) for item in raw_tasks if isinstance(item, dict)
        ]
        if (
            not isinstance(selected_task_ids, list)
            or not selected_task_ids
            or not all(
                isinstance(task_id, str) and task_id
                for task_id in selected_task_ids
            )
            or len(selected_task_ids) != len(set(selected_task_ids))
            or selected_task_ids != ordered_task_ids
            or not isinstance(data.get("source_task_file"), str)
            or not str(data["source_task_file"]).strip()
        ):
            raise ValueError(
                f"{path}: selected C1 catalog must exactly match selected_task_ids "
                "and declare source_task_file"
            )
    elif not deep_ids <= task_ids:
        raise ValueError(f"{path}: expansion_sets.deep30 references unknown task ids")

    by_task: dict[str, tuple[str, ...]] = {}
    capability_tasks: dict[str, set[str]] = {
        capability: set() for capability in _C1_CAPABILITY_VOCABULARY
    }
    capability_benchmarks: dict[str, set[str]] = {
        capability: set() for capability in _C1_CAPABILITY_VOCABULARY
    }
    for item in raw_tasks:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: formal C1 tasks must be mappings")
        task_id = str(item["id"])
        benchmark = str(item.get("benchmark") or "")
        benchmark_tag = _C1_BENCHMARK_TAGS.get(benchmark)
        if benchmark_tag is None:
            raise ValueError(f"{path}: task {task_id} has unsupported benchmark {benchmark!r}")
        expected_tags = ["c1", benchmark_tag]
        if task_id in deep_ids:
            expected_tags.append("deep-chain")
        tags = item.get("tags")
        if tags != expected_tags:
            raise ValueError(
                f"{path}: task {task_id} tags must equal {expected_tags!r}"
            )
        capabilities = item.get("capabilities")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or not all(isinstance(value, str) and value for value in capabilities)
            or len(capabilities) != len(set(capabilities))
        ):
            raise ValueError(
                f"{path}: task {task_id} capabilities must be a non-empty, "
                "unique list in canonical order"
            )
        unknown = set(capabilities) - set(_C1_CAPABILITY_VOCABULARY)
        if unknown:
            raise ValueError(
                f"{path}: task {task_id} has unknown capabilities {sorted(unknown)}"
            )
        if capabilities != sorted(
            capabilities,
            key=_C1_CAPABILITY_VOCABULARY.index,
        ):
            raise ValueError(
                f"{path}: task {task_id} capabilities must use canonical order"
            )
        by_task[task_id] = tuple(capabilities)
        for capability in capabilities:
            capability_tasks[capability].add(task_id)
            capability_benchmarks[capability].add(benchmark)
    if not derived_subset:
        for capability in _C1_CAPABILITY_VOCABULARY:
            if len(capability_tasks[capability]) < 3:
                raise ValueError(
                    f"{path}: capability {capability} covers fewer than three tasks"
                )
            if len(capability_benchmarks[capability]) < 2:
                raise ValueError(
                    f"{path}: capability {capability} covers fewer than two benchmarks"
                )
    return by_task


def load_state_loop_tasks(root: str | Path = Path.cwd(), tasks_path: str | Path = STATE_LOOP_TASKS) -> list[StateLoopTask]:
    base = Path(root).expanduser().resolve()
    path = Path(tasks_path).expanduser()
    if not path.is_absolute():
        path = (base / path).resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("tasks"), list):
        raise ValueError(f"State loop task file must contain a tasks list: {path}")
    task_schema = data.get("task_schema")
    supported_c1_schemas = {
        "c1_v4": 4,
        "c1_v5": 5,
        "c1_lazarus_v1": 1,
    }
    if (
        isinstance(task_schema, str)
        and task_schema.startswith("c1_")
        and task_schema not in supported_c1_schemas
    ):
        raise ValueError(
            f"{path}: unsupported formal C1 task_schema {task_schema!r}; "
            "expected 'c1_v4', 'c1_v5', or 'c1_lazarus_v1'"
        )
    formal_c1 = task_schema in supported_c1_schemas
    if formal_c1 and data.get("schema_version") != supported_c1_schemas[task_schema]:
        raise ValueError(
            f"{path}: formal {task_schema} requires schema_version "
            f"{supported_c1_schemas[task_schema]}"
        )
    if task_schema == "c1_v5":
        validate_c1_v5_catalog(data, source_path=path)
    elif task_schema == "c1_lazarus_v1":
        from .lazarus_catalog import validate_lazarus_catalog

        _, validated_tasks = validate_lazarus_catalog(data, source_path=path)
        data = {**data, "tasks": validated_tasks}
    analysis_capabilities = (
        _validate_c1_analysis_metadata(data, path)
        if formal_c1 and "analysis_schema_version" in data
        else {}
    )
    tasks: list[StateLoopTask] = []
    for item in data["tasks"]:
        if not isinstance(item, dict):
            continue
        legacy_fields = [
            key
            for key in ("start_description", "transition_instruction")
            if key in item
        ]
        if legacy_fields:
            raise ValueError(
                f"{path}: task {item.get('id')} uses unsupported legacy description "
                f"fields: {', '.join(legacy_fields)}"
            )
        explicit_fields = {
            key: str(item.get(key) or "").strip()
            for key in ("preconditions", "reference_steps", "goal_description", "horizon_class")
        }
        if formal_c1:
            required_fields = {
                key: explicit_fields[key]
                for key in ("preconditions", "reference_steps", "horizon_class")
            }
            missing = [key for key, value in required_fields.items() if not value]
            if missing:
                raise ValueError(
                    f"{path}: formal C1 task {item.get('id')} is missing "
                    f"{', '.join(missing)}"
                )
            if "goal_description" in item:
                raise ValueError(
                    f"{path}: formal C1 task {item.get('id')} must not expose "
                    "goal_description"
                )
            expected_state = canonical_state_description(
                explicit_fields["preconditions"],
                explicit_fields["reference_steps"],
            )
            if item.get("state_description") != expected_state:
                raise ValueError(
                    f"{path}: formal C1 task {item.get('id')} state_description "
                    "must equal canonical Preconditions/Reference steps text"
                )
        if formal_c1:
            # horizon_class is retained as a descriptive label only. Formal C1
            # execution uses the uniform budgets in warptest.c1_budgets; this field
            # must still be one of the published labels so reports stay typed.
            if explicit_fields["horizon_class"] not in {"short", "mid", "long"}:
                raise ValueError(
                    f"{path}: formal C1 task {item.get('id')} has invalid horizon_class "
                    f"{explicit_fields['horizon_class']!r}"
                )
        legacy_description = str(item.get("state_description") or item.get("issue") or "").strip()
        preconditions = explicit_fields["preconditions"] or legacy_description
        reference_steps = explicit_fields["reference_steps"] or legacy_description
        goal_description = (
            ""
            if formal_c1
            else explicit_fields["goal_description"] or legacy_description
        )
        horizon_class = explicit_fields["horizon_class"] or "short"
        prompt_revision = str(item.get("prompt_revision") or "").strip()
        state_description = legacy_description or " ".join(
            part for part in (preconditions, reference_steps, goal_description) if part
        )
        if not state_description:
            raise ValueError(f"{path}: task {item.get('id')} is missing state_description")
        oracle_spec = item.get("oracle_spec")
        if not isinstance(oracle_spec, str) or not oracle_spec:
            raise ValueError(f"{path}: task {item.get('id')} is missing oracle_spec")
        if formal_c1:
            _validate_c1_hidden_contract(
                base,
                path,
                item.get("id"),
                oracle_spec,
                lazarus=task_schema == "c1_lazarus_v1",
            )
        issue_item = {**item, "issue": state_description}
        issue = PublicIssue.from_mapping(issue_item, source_path=path)
        tasks.append(
            StateLoopTask(
                issue=issue,
                oracle_spec=oracle_spec,
                state_description=state_description,
                preconditions=preconditions,
                reference_steps=reference_steps,
                goal_description=goal_description,
                horizon_class=horizon_class,
                prompt_revision=prompt_revision,
                capabilities=(
                    tuple(str(value) for value in item.get("capabilities", []))
                    if task_schema == "c1_lazarus_v1"
                    else analysis_capabilities.get(issue.id, ())
                ),
                coordinator_metadata=(
                    {"defect": copy.deepcopy(item["defect"])}
                    if task_schema == "c1_lazarus_v1"
                    else {}
                ),
            )
        )
    return tasks


def _apply_experiment_seed(
    spec: dict[str, Any], seed: int | None, *, benchmark: str
) -> dict[str, Any]:
    """Attach the formal experiment seed without exposing grader-only state.

    Seeding is idempotent, so this stays safe on a spec the shared acceptance
    already seeded.
    """

    return seed_generated_spec(spec, seed, benchmark=benchmark)


def generated_spec_hash(generated_spec: Any) -> str:
    """Hash the whole seeded spec the way the synthesized GUI arm does.

    ``state_fingerprint`` covers only ``benchmark``/``target``/``checkpoint``, is
    truncated, and is salted with the repo commit, so it cannot be compared
    against the GUI arm's ``generated_spec_hash``. Recording this alongside it
    lets the report prove both arms restored the same self-synthesized start.
    """

    if not isinstance(generated_spec, dict) or not generated_spec:
        return "no-usable-model-spec"
    return canonical_hash(generated_spec)


def _accepted_state_spec(
    candidate: Any, *, task: StateLoopTask, seed: int | None
) -> tuple[dict[str, Any] | None, str]:
    """Grade one candidate with the accept-or-reject decision both arms share.

    The state arm used to gate each backend with its own predicate -- most of
    them omitted ``task=`` and therefore only checked that the four required keys
    existed, while the GUI arm always ran the strict C1 contract. Identical model
    output could then be a graded trial on one arm and a fair zero on the other.
    The spec returned here is already seeded.
    """

    acceptance = accept_c1_generated_spec(
        candidate,
        issue=task.issue,
        benchmark=task.benchmark,
        seed=seed,
    )
    if acceptance.accepted:
        return acceptance.spec, ""
    return None, str(acceptance.reason or "generated_spec was rejected")


def _bind_task_random_control(
    result: dict[str, Any],
    task: StateLoopTask,
) -> None:
    control = copy.deepcopy(getattr(task.issue, "random_control", {}) or {})
    result["random_control"] = control
    evidence = result.get("seed_evidence")
    if not isinstance(evidence, dict):
        evidence = {
            "schema_version": 1,
            "source": "runtime_not_started",
            "observed": False,
            "benchmark": getattr(task, "benchmark", result.get("benchmark", "")),
        }
        result["seed_evidence"] = evidence
    evidence["random_control"] = copy.deepcopy(control)
    mode = control.get("mode")
    if mode == "not_applicable":
        evidence["randomness"] = "not_applicable"
        evidence["seed_role"] = "trial_pairing_only"
        evidence.setdefault("seed", result.get("seed"))
        valid = bool(
            evidence.get("observed") is True
            and evidence.get("randomness") == "not_applicable"
            and evidence.get("seed_role") == "trial_pairing_only"
            and evidence.get("seed") == result.get("seed")
        )
    else:
        valid = bool(
            mode == "experiment_seed"
            and evidence.get("observed") is True
            and evidence.get("algorithm") == control.get("algorithm")
            and evidence.get("seed") == result.get("seed")
            and result.get("seed") in (control.get("seeds") or [])
        )
    result["random_control_evidence_valid"] = valid


@dataclass
class OracleGoldens:
    before: str
    after: str
    fingerprint: str
    cache_hit: bool
    cacheable: bool
    consistency: dict[str, Any] = field(default_factory=dict)
    quality: dict[str, Any] = field(default_factory=dict)
    # Producer-time byte binding.  Trial snapshots hash this report payload, so
    # later coordinated edits to both meta.json and its PNGs remain detectable.
    artifacts: dict[str, dict[str, str]] = field(default_factory=dict)
    # Oracle-authored visual guidance for the (possibly nondeterministic) after state.
    after_description: str | None = None
    after_rubric: dict[str, list[str]] | None = None


class UnityOracleInfrastructureError(RuntimeError):
    """A Unity oracle failed before policy execution for a runtime reason."""

    infrastructure_error = True


def _unity_oracle_pair_infrastructure_failure(pair: dict[str, Any]) -> bool:
    """Recognize hard Unity runtime/capture failures without masking bad specs."""

    for stage in ("before", "after"):
        result = pair.get(stage)
        if not isinstance(result, dict) or result.get("status") == "success":
            continue
        exit_code = result.get("exit_code")
        if (
            isinstance(exit_code, int)
            and not isinstance(exit_code, bool)
            and exit_code != 0
        ):
            return True
        utility = result.get("utility_report")
        detail = (
            str(utility.get("detail") or "").lower()
            if isinstance(utility, dict)
            else ""
        )
        if any(
            marker in detail
            for marker in (
                "did not write a report",
                "failed to launch",
                "timed out",
                "process exited",
                "runtime unavailable",
            )
        ):
            return True
        if result.get("live_source") == "capture_failure":
            return True
    return False


def oracle_goldens_report_payload(goldens: OracleGoldens | Any) -> dict[str, Any]:
    """Serialize a golden pair with provenance and producer-time byte digests."""

    return {
        "before": goldens.before,
        "after": goldens.after,
        "fingerprint": getattr(goldens, "fingerprint", None),
        "cache_hit": bool(getattr(goldens, "cache_hit", False)),
        "cacheable": bool(getattr(goldens, "cacheable", False)),
        "consistency": dict(getattr(goldens, "consistency", {}) or {}),
        "quality": dict(getattr(goldens, "quality", {}) or {}),
        "artifacts": copy.deepcopy(getattr(goldens, "artifacts", {}) or {}),
    }


# Generic before-state rubric: the restored start state is deterministic, so we only
# guard against a blank/crashed page and ignore appear-animation jitter.
BEFORE_RUBRIC: dict[str, list[str]] = {
    "must": ["The restored application UI or document/game state is visible and not blank."],
    "must_not": ["A crash, browser/application error, or blank page/window is visible.", "A different application or wrong screen is shown."],
    "ignore": ["Minor animation/position differences.", "Small transient UI or cursor differences."],
}

# A rendered voxel world perturbs far more than the generic rubric tolerates, so
# grading Luanti against BEFORE_RUBRIC rejects correct restores. This used to
# live in the GUI arm only, which meant the two arms judged the same Luanti Start
# against different rubrics.
LUANTI_BEFORE_RUBRIC: dict[str, list[str]] = {
    "must": [
        "The rendered Luanti voxel world and its normal game HUD are visible and not blank."
    ],
    "must_not": [
        "A crash, application error, blank window, or Luanti main menu is visible.",
        "A different application or non-game screen is shown.",
    ],
    "ignore": [
        "Day/night lighting, sky, cloud, weather, water-animation, and shadow differences.",
        "Transient server version, join, chat, status, debug, or HUD text.",
        "Minor camera, cursor, player, wielded-tool, and animation differences that do not replace the voxel scene.",
    ],
}


def before_rubric_for_benchmark(benchmark: Any) -> dict[str, list[str]]:
    """Return the one Start rubric every arm must use for this benchmark."""

    if canonical_c1_benchmark(benchmark) == "luanti":
        return LUANTI_BEFORE_RUBRIC
    return BEFORE_RUBRIC


def _oracle_visual_guidance(oracle_spec: dict[str, Any]) -> tuple[str | None, dict[str, list[str]] | None]:
    """Pull the after-state description/rubric from the oracle spec's visual_oracle.

    The hidden oracle already authors what must/must-not be visible after the smoke
    actions and what to ignore (e.g. 2048's random spawn), so the after-half judge
    reuses it instead of demanding pixel-exact equality with the golden.

    An oracle may supply a dedicated ``after_description`` / ``after_rubric`` when the
    post-action state must be judged more strictly than the shared start-state rubric
    (e.g. OpenRA production tasks that must show the newly built factory and unit). When
    present these take precedence; otherwise the generic ``description`` / ``rubric``
    apply to both halves.
    """

    visual = visual_oracle(oracle_spec)
    if not isinstance(visual, dict):
        return None, None
    description = visual.get("after_description") or visual.get("description")
    if not isinstance(description, str):
        description = None
    rubric_raw = visual.get("after_rubric")
    if not isinstance(rubric_raw, dict):
        rubric_raw = visual.get("rubric") if isinstance(visual.get("rubric"), dict) else None
    rubric: dict[str, list[str]] | None = None
    if rubric_raw is not None:
        rubric = {
            key: [str(item) for item in rubric_raw.get(key, []) if isinstance(item, str)]
            for key in ("must", "must_not", "ignore")
        }
    return description, rubric


def _oracle_provenance_spec(oracle_spec: dict[str, Any]) -> dict[str, Any]:
    """Return the canonical on-disk shape used for golden provenance.

    Evaluation-v1 specs are materialized with root assertions and visual_oracle
    fields for legacy runtimes.  Those fields are derived from evaluation and
    must not create a second cache identity or disagree with artifact verifiers
    that hash the validated YAML source.
    """

    canonical = copy.deepcopy(oracle_spec)
    if evaluation_contract(canonical) is not None:
        canonical.pop("assertions", None)
        canonical.pop("visual_oracle", None)
    return canonical


def _oracle_fingerprint(repo_commit: str | None, oracle_spec: dict[str, Any]) -> str:
    provenance_spec = _oracle_provenance_spec(oracle_spec)
    blob = yaml.safe_dump(
        {
            "schema_version": ORACLE_GOLDEN_CACHE_SCHEMA_VERSION,
            "commit": repo_commit,
            # Keep the experiment seed explicit in the cache identity.  It is also
            # present in the seeded spec, but the separate field makes accidental
            # use of an unseeded spec visible in metadata and tests.
            "experiment_seed": provenance_spec.get("experiment_seed"),
            "spec": provenance_spec,
        },
        sort_keys=True,
        allow_unicode=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _oracle_spec_digest(oracle_spec: dict[str, Any]) -> str:
    blob = yaml.safe_dump(
        _oracle_provenance_spec(oracle_spec),
        sort_keys=True,
        allow_unicode=False,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _file_digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        return None


def _cached_oracle_goldens(
    repo_commit: str | None,
    oracle_spec: dict[str, Any],
    *,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens | None:
    """Return a fully verified shared golden pair without starting a runtime.

    A directory name alone is not accepted as provenance.  Reuse requires a real
    repository commit, matching schema/fingerprint/spec/seed metadata, matching
    image digests, and two currently informative screenshots.  Legacy cache
    entries therefore fail closed and are regenerated once under the new schema.
    """

    if not isinstance(repo_commit, str) or not repo_commit.strip():
        return None
    fingerprint = _oracle_fingerprint(repo_commit, oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    golden_before = cache_dir / "golden_before.png"
    golden_after = cache_dir / "golden_after.png"
    metadata_path = cache_dir / "meta.json"
    if not (golden_before.is_file() and golden_after.is_file() and metadata_path.is_file()):
        return None
    if any(path.is_symlink() for path in (golden_before, golden_after, metadata_path)):
        return None
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("schema_version") != ORACLE_GOLDEN_CACHE_SCHEMA_VERSION:
        return None
    if metadata.get("cacheable") is not True:
        return None
    if metadata.get("fingerprint") != fingerprint:
        return None
    if metadata.get("git_commit") != repo_commit:
        return None
    if metadata.get("experiment_seed") != oracle_spec.get("experiment_seed"):
        return None
    if metadata.get("oracle_spec_sha256") != _oracle_spec_digest(oracle_spec):
        return None
    images = metadata.get("images")
    if not isinstance(images, dict):
        return None
    before_meta = images.get("before")
    after_meta = images.get("after")
    if not isinstance(before_meta, dict) or not isinstance(after_meta, dict):
        return None
    if before_meta.get("sha256") != _file_digest(golden_before):
        return None
    if after_meta.get("sha256") != _file_digest(golden_after):
        return None
    before_quality = screenshot_quality(golden_before)
    after_quality = screenshot_quality(golden_after)
    if not before_quality.get("informative") or not after_quality.get("informative"):
        return None
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    return OracleGoldens(
        before=str(golden_before),
        after=str(golden_after),
        fingerprint=fingerprint,
        cache_hit=True,
        cacheable=True,
        consistency={"source": "verified_cache"},
        quality={"before": before_quality, "after": after_quality},
        artifacts=capture_oracle_golden_artifact_digests(golden_before, golden_after),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def lookup_cached_oracle_goldens(
    repo: str | Path,
    seeded_oracle_spec: dict[str, Any],
    *,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens | None:
    """Cheap pre-runtime lookup for GUI/state-loop call sites.

    ``seeded_oracle_spec`` must be the exact hidden spec that would be passed to
    :func:`ensure_oracle_goldens`.  The function performs only a git revision read,
    metadata/digest checks, and screenshot quality checks; it never starts a
    browser, Luanti client, or other application runtime.
    """

    resolved_repo = Path(repo).expanduser().resolve()
    repo_commit = _oracle_repo_identity(resolved_repo, seeded_oracle_spec)
    return _cached_oracle_goldens(
        repo_commit,
        seeded_oracle_spec,
        cache_root=cache_root,
    )


def _write_oracle_golden_metadata(
    directory: Path,
    *,
    repo_commit: str | None,
    oracle_spec: dict[str, Any],
    benchmark: str | None,
    oracle_spec_path: Path,
    consistency: dict[str, Any],
    quality: dict[str, Any],
    cacheable: bool,
) -> None:
    before = directory / "golden_before.png"
    after = directory / "golden_after.png"
    before_digest = _file_digest(before)
    after_digest = _file_digest(after)
    if before_digest is None or after_digest is None:
        raise RuntimeError("oracle golden images could not be hashed for cache metadata")
    payload = {
        "schema_version": ORACLE_GOLDEN_CACHE_SCHEMA_VERSION,
        "fingerprint": _oracle_fingerprint(repo_commit, oracle_spec),
        "git_commit": repo_commit,
        "experiment_seed": oracle_spec.get("experiment_seed"),
        "oracle_spec_sha256": _oracle_spec_digest(oracle_spec),
        "benchmark": benchmark,
        "oracle_spec_path": str(oracle_spec_path),
        "consistency": consistency,
        "quality": quality,
        "cacheable": bool(cacheable),
        "images": {
            "before": {"sha256": before_digest},
            "after": {"sha256": after_digest},
        },
    }
    metadata_path = directory / "meta.json"
    temporary_path = metadata_path.with_name(
        f"{metadata_path.name}.tmp-{os.getpid()}-{time.time_ns()}"
    )
    temporary_path.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(temporary_path, metadata_path)


def ensure_oracle_goldens(
    session: StateSession,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    *,
    cache_root: str | Path = ".warptest-cache",
    cross_validate_captures: int = 2,
) -> OracleGoldens:
    """Produce (or reuse cached) before/after oracle goldens for a hidden spec.

    Runs the hidden oracle spec on the warm session: a before-pass (no smoke) and an
    after-pass (with smoke). To guard golden correctness the before state is captured
    several times and required to be consistently informative before it is cached.
    """

    cached = _cached_oracle_goldens(
        session.git_commit,
        oracle_spec,
        cache_root=cache_root,
    )
    if cached is not None:
        return cached

    fingerprint = _oracle_fingerprint(session.git_commit, oracle_spec)
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    local_cache_dir = session.report_dir / "oracle-goldens" / fingerprint

    actions = oracle_spec.get("actions", [])
    captures: list[str] = []
    capture_qualities: list[dict[str, Any]] = []
    failures: list[str] = []
    for index in range(max(1, cross_validate_captures)):
        outcome = session.restore(
            oracle_spec, oracle_spec_path, actions=[], screenshot_name=f"oracle-before-{index + 1:02d}.png"
        )
        if outcome.status != "success":
            failures.append(f"before capture {index + 1} failed: {outcome.detail}")
            continue
        if not outcome.screenshot:
            failures.append(f"before capture {index + 1} did not produce a screenshot")
            continue
        quality = screenshot_quality(Path(outcome.screenshot))
        if not quality["informative"]:
            failures.append(f"before capture {index + 1} is non-informative: {quality}")
            continue
        captures.append(outcome.screenshot)
        capture_qualities.append(quality)
    if len(captures) < max(1, cross_validate_captures):
        detail = "; ".join(failures) if failures else "not all before captures were informative"
        raise RuntimeError(f"Oracle before-state could not be captured for golden generation: {detail}")

    after_outcome = session.restore(
        oracle_spec,
        oracle_spec_path,
        actions=actions,
        screenshot_name="oracle-after.png",
        require_after=True,
    )
    if after_outcome.status != "success" or not after_outcome.screenshot:
        raise RuntimeError(f"Oracle after-state could not be captured for golden generation: {after_outcome.detail}")
    after_quality = screenshot_quality(Path(after_outcome.screenshot))
    if not after_quality["informative"]:
        raise RuntimeError(f"Oracle after-state screenshot is non-informative: {after_quality}")

    consistency = _capture_consistency(captures)
    # A shared cache entry must be bound to an immutable repository revision.
    # Non-git repos remain usable, but their captures stay report-local.
    cacheable = bool(consistency.get("byte_identical") and session.git_commit)
    target_dir = cache_dir if cacheable else local_cache_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_before = target_dir / "golden_before.png"
    target_after = target_dir / "golden_after.png"
    shutil.copyfile(captures[0], target_before)
    shutil.copyfile(after_outcome.screenshot, target_after)
    quality = {"before": capture_qualities[0], "after": after_quality}
    _write_oracle_golden_metadata(
        target_dir,
        repo_commit=session.git_commit,
        oracle_spec=oracle_spec,
        benchmark=session.benchmark,
        oracle_spec_path=oracle_spec_path,
        consistency=consistency,
        quality=quality,
        cacheable=cacheable,
    )
    return OracleGoldens(
        before=str(target_before),
        after=str(target_after),
        fingerprint=fingerprint,
        cache_hit=False,
        cacheable=cacheable,
        consistency=consistency,
        quality=quality,
        artifacts=capture_oracle_golden_artifact_digests(target_before, target_after),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def _capture_consistency(paths: list[str]) -> dict[str, Any]:
    digests = []
    for path in paths:
        try:
            digests.append(hashlib.sha256(Path(path).read_bytes()).hexdigest())
        except Exception:
            digests.append(None)
    unique = {digest for digest in digests if digest is not None}
    return {
        "captures": len(paths),
        "unique_digests": len(unique),
        "byte_identical": len(unique) == 1 and len(digests) > 1,
    }


def _image_fingerprint(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]
    except Exception:
        return None


def _sha256_json(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _stable_pair_start_id(
    namespace: str, model: str, seed: int, task_id: str
) -> str:
    """Return the run-scoped pair identity used by E/E-prime core auditing.

    This intentionally is not a content digest.  Artifact/content SHA values
    remain useful diagnostics, but comparison validity follows the shared
    orchestrator identity and observed Start/treatment boundaries.
    """

    return f"{namespace}:{model}:{int(seed)}:{task_id}"


STATE_SYNTHESIS_CACHE_CONSUMER = C1_SYNTHESIS_CONSUMER


class CachedSynthesisAgent:
    """Wrap the synthesis agent so a recovery pass reuses what it already paid for.

    Arm D caches its self-synthesized start; without the same cache, arm E
    re-rolls the synthesis LLM on every recovery pass and its Start number moves
    for reasons unrelated to the method.  Entries were once keyed per consumer,
    which meant the two arms restored different model-generated starts for the
    same task, model and seed; they now share one consumer and one accepted spec.

    The wrapped call runs under a nested cost meter, which separates the
    synthesis burden from the rest of the trial and lets a cache hit report zero
    actual spend alongside the full counterfactual attribution.
    """

    def __init__(
        self,
        agent: Any,
        *,
        cache_root: Path | None,
        base: Path,
        seed: int,
        synth_model: str,
        provider_name: str,
        provider_base_url: str,
        temperature: float,
        max_tokens: int,
        explore: bool,
        max_explore_steps: int,
        consumer: str = STATE_SYNTHESIS_CACHE_CONSUMER,
    ) -> None:
        self._agent = agent
        self._cache_root = cache_root
        self._base = Path(base)
        self._seed = int(seed)
        self._synth_model = synth_model
        self._provider_name = provider_name
        self._provider_base_url = provider_base_url
        self._temperature = float(temperature)
        self._max_tokens = int(max_tokens)
        self._explore = bool(explore)
        self._max_explore_steps = int(max_explore_steps)
        self._consumer = consumer
        self._task: StateLoopTask | None = None
        self._ledger: list[dict[str, Any]] = []
        self._pending_error: BaseException | None = None

    @property
    def name(self) -> str:
        return getattr(self._agent, "name", "synthesis-agent")

    def __getattr__(self, item: str) -> Any:
        return getattr(self._agent, item)

    def bind_task(self, task: StateLoopTask | None) -> None:
        self._task = task
        self._ledger = []
        self._pending_error = None

    @property
    def current_cache_fingerprint(self) -> str | None:
        if not self._ledger:
            return None
        value = self._ledger[-1].get("cache_fingerprint")
        return str(value) if isinstance(value, str) and value else None

    def drain(self) -> dict[str, Any]:
        """Return this task's synthesis cost ledger and clear it."""

        entries, self._ledger = self._ledger, []
        empty = cost.CostMeter().summary()
        if not entries:
            return {
                "synthesis_actual_cost": empty,
                "synthesis_attributed_cost": empty,
                "synthesis_transient_actual_cost": empty,
                "checkpoint_synthesis_ms": None,
                "cache_hit": None,
                "cache_fingerprint": None,
            }
        return {
            "synthesis_actual_cost": cost.merge_summaries(
                [entry["actual_cost"] for entry in entries]
            ),
            "synthesis_attributed_cost": cost.merge_summaries(
                [entry["attributed_cost"] for entry in entries]
            ),
            "synthesis_transient_actual_cost": empty,
            "checkpoint_synthesis_ms": sum(
                int(entry["checkpoint_synthesis_ms"]) for entry in entries
            ),
            "cache_hit": all(bool(entry["cache_hit"]) for entry in entries),
            "cache_fingerprint": entries[-1]["cache_fingerprint"],
        }

    def synthesize(
        self,
        task: PublicIssue,
        root: str | Path = Path.cwd(),
        artifact_dir: Path | None = None,
        *,
        feedback: dict[str, Any] | None = None,
    ) -> Any:
        # Feedback-driven retries depend on the prior iteration's verdict, which
        # is not part of the key, so they always re-synthesize.
        if self._task is None or self._cache_root is None or feedback is not None:
            checkpoint = self._run(
                task, root, artifact_dir, feedback=feedback, cache_hit=False
            )["checkpoint"]
            if self._task is None:
                return checkpoint
            return self._settle_outcome(self._accepted_outcome(checkpoint))

        cache_key = synthesis_cache_key(
            consumer=self._consumer,
            identity_root=cache_identity_root(
                self._base, lambda base: resolve_task_repo(base, task)
            ),
            public_task={
                "public_issue": task.public_payload(),
                "preconditions": str(
                    getattr(self._task, "preconditions", "")
                    or self._task.state_description
                ),
                "reference_steps": str(
                    getattr(self._task, "reference_steps", "")
                    or self._task.state_description
                ),
            },
            seed=self._seed,
            synth_model=self._synth_model,
            model_request_profile=model_request_profile_provenance(
                self._synth_model
            ),
            provider_name=self._provider_name,
            provider_base_url=self._provider_base_url,
            temperature=self._temperature,
            max_tokens=self._max_tokens,
            explore=self._explore,
            max_explore_steps=self._max_explore_steps,
            ablation_identity=(
                {
                    "variant": self._agent.ablation_variant,
                    "revision": ABLATION_REVISION,
                    "sources_sha256": hashlib.sha256(b"".join(
                        (Path(__file__).parent / name).read_bytes()
                        for name in ("synthesis_ablation.py", "synthesis.py", "spec_schema.py", "task_schema.py")
                    )).hexdigest(),
                }
                if getattr(self._agent, "ablation_variant", "full") != "full" else None
            ),
        )
        fingerprint = canonical_hash(cache_key, length=32)

        def produce() -> dict[str, Any]:
            started = time.perf_counter()
            self._pending_error = None
            with cost.scoped_meter() as meter:
                try:
                    checkpoint = self._agent.synthesize(
                        task, root, artifact_dir, feedback=feedback
                    )
                    outcome = self._accepted_outcome(checkpoint)
                except SynthesisError as error:
                    outcome = synthesis_outcome(
                        status="fair_failure",
                        synth_status="synthesis_error",
                        generated_spec=None,
                        checkpoint=None,
                        used_public_scaffold=False,
                        reason=str(error),
                        artifacts=dict(error.artifacts),
                    )
                except BaseException as error:  # noqa: BLE001 - re-raised below
                    self._pending_error = error
                    outcome = synthesis_outcome(
                        status="infrastructure_error",
                        synth_status="synthesis_error",
                        generated_spec=None,
                        checkpoint=None,
                        used_public_scaffold=False,
                        reason=str(error),
                    )
            return {
                "schema_version": SELF_SYNTHESIS_CACHE_SCHEMA_VERSION,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "cache_key": cache_key,
                "cache_key_fingerprint": fingerprint,
                "producer": {"consumer": self._consumer},
                "checkpoint_synthesis_ms": int(
                    (time.perf_counter() - started) * 1000
                ),
                "synthesis_cost": meter.summary(),
                "outcome": outcome,
            }

        payload = obtain_synthesis(
            cache_key=cache_key,
            fingerprint=fingerprint,
            shared_cache_root=self._cache_root,
            produce=produce,
            materialize=lambda entry, *, cache_hit, cache_path: {
                "payload": entry,
                "cache_hit": cache_hit,
            },
            outcome_usable=synthesis_outcome_usable,
        )
        return self._settle(payload["payload"], cache_hit=payload["cache_hit"])

    def _accepted_outcome(self, checkpoint: Any) -> dict[str, Any]:
        """Grade one synthesized checkpoint with the shared acceptance decision.

        The state arm used to validate the *unseeded* candidate with a permissive
        predicate while the GUI arm validated the seeded spec strictly, so the
        same model output could be a graded trial on one arm and a fair zero on
        the other.
        """

        task = self._task
        assert task is not None
        used_public_scaffold = bool(getattr(checkpoint, "used_public_scaffold", False))
        acceptance = accept_c1_generated_spec(
            getattr(checkpoint, "model_generated_spec", None),
            issue=task.issue,
            benchmark=task.benchmark,
            seed=self._seed,
        )
        if not acceptance.accepted:
            return synthesis_outcome(
                status="fair_failure",
                synth_status=acceptance.status,
                generated_spec=None,
                checkpoint=None,
                used_public_scaffold=used_public_scaffold,
                reason=acceptance.reason,
                artifacts={"acceptance_errors": list(acceptance.errors)},
            )
        return synthesis_outcome(
            status="success",
            synth_status="generated",
            generated_spec=acceptance.spec,
            checkpoint=checkpoint_payload(checkpoint, acceptance.spec),
            used_public_scaffold=used_public_scaffold,
            reason=None,
        )

    def _run(
        self,
        task: PublicIssue,
        root: str | Path,
        artifact_dir: Path | None,
        *,
        feedback: dict[str, Any] | None,
        cache_hit: bool,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        with cost.scoped_meter() as meter:
            try:
                checkpoint = self._agent.synthesize(
                    task, root, artifact_dir, feedback=feedback
                )
            finally:
                summary = meter.summary()
                self._record(
                    actual_cost=summary,
                    attributed_cost=summary,
                    checkpoint_synthesis_ms=int(
                        (time.perf_counter() - started) * 1000
                    ),
                    cache_hit=cache_hit,
                    cache_fingerprint=None,
                )
        return {"checkpoint": checkpoint}

    def _settle(self, payload: dict[str, Any], *, cache_hit: bool) -> Any:
        empty = cost.CostMeter().summary()
        attributed = dict(payload["synthesis_cost"])
        self._record(
            actual_cost=empty if cache_hit else attributed,
            attributed_cost=attributed,
            checkpoint_synthesis_ms=int(payload["checkpoint_synthesis_ms"]),
            cache_hit=cache_hit,
            cache_fingerprint=payload["cache_key_fingerprint"],
        )
        return self._settle_outcome(payload["outcome"])

    def _settle_outcome(self, outcome: dict[str, Any]) -> Any:
        status = outcome.get("status")
        if status == "success":
            return MethodCheckpoint(**outcome["checkpoint"])
        if status == "fair_failure":
            raise SynthesisError(
                str(outcome.get("reason") or "checkpoint synthesis failed"),
                artifacts=dict(outcome.get("artifacts") or {}),
            )
        error = self._pending_error
        self._pending_error = None
        raise error if error is not None else RuntimeError(
            str(outcome.get("reason") or "checkpoint synthesis failed")
        )

    def _record(
        self,
        *,
        actual_cost: dict[str, Any],
        attributed_cost: dict[str, Any],
        checkpoint_synthesis_ms: int,
        cache_hit: bool,
        cache_fingerprint: str | None,
    ) -> None:
        self._ledger.append(
            {
                "actual_cost": actual_cost,
                "attributed_cost": attributed_cost,
                "checkpoint_synthesis_ms": checkpoint_synthesis_ms,
                "cache_hit": cache_hit,
                "cache_fingerprint": cache_fingerprint,
            }
        )


def _apply_state_synthesis_accounting(result: dict[str, Any], agent: Any) -> None:
    """Split the synthesis burden out of one trial's task cost.

    ``cost`` stays the fair comparison burden (task spend plus the
    counterfactual synthesis attribution) while ``synthesis_actual_cost``
    records what this attempt actually paid, which is zero on a cache hit.
    """

    empty = cost.CostMeter().summary()
    if not isinstance(agent, CachedSynthesisAgent):
        result.setdefault("synthesis_actual_cost", empty)
        result.setdefault("synthesis_attributed_cost", empty)
        result.setdefault("synthesis_transient_actual_cost", empty)
        result.setdefault("synthesis_cache_hit", False)
        result.setdefault("synthesis_cache_fingerprint", None)
        return

    ledger = agent.drain()
    result["synthesis_actual_cost"] = ledger["synthesis_actual_cost"]
    result["synthesis_attributed_cost"] = ledger["synthesis_attributed_cost"]
    result["synthesis_transient_actual_cost"] = ledger[
        "synthesis_transient_actual_cost"
    ]
    result["synthesis_cache_hit"] = bool(ledger["cache_hit"])
    result["synthesis_cache_fingerprint"] = ledger["cache_fingerprint"]
    counterfactual_ms = ledger["checkpoint_synthesis_ms"]
    if counterfactual_ms is not None:
        # A cache hit costs no wall time, so the reported synthesis latency has
        # to come from the entry rather than from this attempt's clock.
        result["checkpoint_synthesis_ms"] = int(counterfactual_ms)


def _attempt_hash_payload(
    attempt: dict[str, Any], *, task_id: str
) -> dict[str, Any]:
    return {
        "task_id": str(task_id),
        "attempt_ordinal": attempt.get("attempt_ordinal"),
        "attempt_status": attempt.get("attempt_status"),
        "retry_category": attempt.get("retry_category"),
        "retry_reason": attempt.get("retry_reason"),
        "previous_attempt_hash": attempt.get("previous_attempt_hash"),
        "task_actual_cost": attempt.get("task_actual_cost"),
        # State-loop synthesis is part of task_actual_cost. Keep the explicit
        # null field so GUI/state attempt hashes share one auditable schema.
        "synthesis_accounting": attempt.get("synthesis_accounting"),
    }


def _bind_attempt_ledger(
    attempts: list[dict[str, Any]], *, task_id: str
) -> tuple[list[dict[str, Any]], str]:
    bound: list[dict[str, Any]] = []
    previous_hash: str | None = None
    for ordinal, raw_attempt in enumerate(attempts, start=1):
        attempt = dict(raw_attempt)
        attempt["attempt_ordinal"] = ordinal
        attempt["previous_attempt_hash"] = previous_hash
        attempt["attempt_hash"] = _sha256_json(
            _attempt_hash_payload(attempt, task_id=task_id)
        )
        previous_hash = attempt["attempt_hash"]
        bound.append(attempt)
    ledger_hash = _sha256_json(
        {
            "task_id": str(task_id),
            "attempt_hashes": [attempt["attempt_hash"] for attempt in bound],
        }
    )
    return bound, ledger_hash


def _validated_attempt_ledger(
    result: dict[str, Any], *, task_id: str, incomplete: bool
) -> list[dict[str, Any]]:
    raw_attempts = result.get("attempt_costs")
    if not isinstance(raw_attempts, list) or not raw_attempts:
        raise cost.UsageAccountingError(
            f"cannot safely resume {task_id}: missing hash-bound attempt_costs"
        )
    attempts = [dict(value) for value in raw_attempts if isinstance(value, dict)]
    if len(attempts) != len(raw_attempts):
        raise cost.UsageAccountingError(
            f"cannot safely resume {task_id}: malformed attempt_costs"
        )
    if int(result.get("attempt_count", 0) or 0) != len(attempts):
        raise cost.UsageAccountingError(
            f"cannot safely resume {task_id}: attempt_count mismatch"
        )
    rebound, ledger_hash = _bind_attempt_ledger(attempts, task_id=task_id)
    for index, (original, expected) in enumerate(
        zip(attempts, rebound, strict=True)
    ):
        if index < len(attempts) - 1:
            expected_status = "retryable_infrastructure"
        elif not incomplete:
            expected_status = "treatment"
        elif result.get("retryable_incomplete") is True:
            expected_status = "retryable_infrastructure"
        elif cost.contains_unknown_usage(result):
            expected_status = "unknown_usage"
        else:
            expected_status = "nonretryable_incomplete"
        if original.get("attempt_ordinal") != expected["attempt_ordinal"]:
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: attempt ordinal mismatch"
            )
        if original.get("attempt_status") != expected_status:
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: invalid prior attempt status"
            )
        retry_category = original.get("retry_category")
        retry_reason = original.get("retry_reason")
        if expected_status == "retryable_infrastructure":
            if retry_category not in {"infra", "rate_limit"}:
                raise cost.UsageAccountingError(
                    f"cannot safely resume {task_id}: invalid retry category"
                )
            if not isinstance(retry_reason, str) or not retry_reason.strip():
                raise cost.UsageAccountingError(
                    f"cannot safely resume {task_id}: missing retry reason"
                )
            if (
                run_context.error_category(RuntimeError(retry_reason))
                != retry_category
            ):
                raise cost.UsageAccountingError(
                    f"cannot safely resume {task_id}: retry reason/category mismatch"
                )
        elif retry_category is not None or retry_reason is not None:
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: terminal attempt has retry metadata"
            )
        if original.get("previous_attempt_hash") != expected["previous_attempt_hash"]:
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: attempt hash chain mismatch"
            )
        if original.get("attempt_hash") != expected["attempt_hash"]:
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: attempt hash mismatch"
            )
        if not isinstance(original.get("task_actual_cost"), dict):
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: missing task_actual_cost"
            )
        accounting = original.get("synthesis_accounting")
        if not isinstance(accounting, dict):
            raise cost.UsageAccountingError(
                f"cannot safely resume {task_id}: missing state synthesis_accounting"
            )
        for accounting_field in (
            "attributed_cost",
            "actual_cost",
            "transient_actual_cost",
        ):
            if not isinstance(accounting.get(accounting_field), dict):
                raise cost.UsageAccountingError(
                    f"cannot safely resume {task_id}: "
                    f"state synthesis_accounting.{accounting_field} is missing"
                )
    if result.get("attempt_ledger_hash") != ledger_hash:
        raise cost.UsageAccountingError(
            f"cannot safely resume {task_id}: attempt ledger hash mismatch"
        )
    return attempts


def _resolve_task_timeout_s(task: Any, default_timeout_s: float) -> float:
    """Return one task's wall-clock deadline, preferring its published budget."""

    budget_ms = c1_task_budget_ms(
        task, default_ms=int(round(float(default_timeout_s) * 1000.0))
    )
    return validate_task_timeout_s(budget_ms / 1000.0)


def run_state_loop_benchmark(
    root: str | Path = Path.cwd(),
    *,
    tasks_path: str | Path = STATE_LOOP_TASKS,
    agent_kind: str = "llm",
    model: str = DEFAULT_SYNTHESIS_MODEL,
    base_url: str = "https://api-inference.modelscope.cn/v1",
    api_key_env: str = "MODELSCOPE_API_KEY",
    temperature: float = 0.2,
    # Shared with the synthesized GUI arm: an unequal synthesis budget silently
    # splits the shared checkpoint cache and makes the two Start rates
    # incomparable.
    max_tokens: int = C1_SYNTHESIS_MAX_TOKENS,
    explore: bool = C1_SYNTHESIS_EXPLORE,
    max_explore_steps: int = 12,
    # Run-scoped checkpoint cache, mirroring the synthesized GUI arm so a
    # recovery pass reuses the checkpoint instead of re-rolling the model.
    shared_synthesis_cache: str | Path | None = None,
    max_iters: int = 1,
    seeds: int = 1,
    seed: int | None = None,
    headless: bool = True,
    background_browser_headless: bool = False,
    report_dir: Path | None = None,
    run_meta: dict[str, Any] | None = None,
    resume: bool = False,
    restore_diagnostics: bool = True,
    restore_diagnostic_task_ids: list[str] | tuple[str, ...] | None = None,
    inner_workers: int = 1,
    task_timeout_s: float = DEFAULT_C1_TASK_TIMEOUT_S,
    sandbox_manager: C1SandboxManager | None = None,
    invocation_id: str = "state-loop",
    unity_serialized: bool = False,
    unity_serialized_benchmarks: Sequence[str] | None = None,
    expected_lane_plan: list[list[dict[str, Any]]] | None = None,
    expected_task_plan: list[dict[str, Any]] | None = None,
    global_stop_event: C1GlobalStop | threading.Event | None = None,
    ready_scheduler: C1ReadyScheduler | None = None,
    ready_outer_ordinal: int = 0,
    ready_handle: C1ReadyHandle | None = None,
    ready_ticket: C1ReadyTicket | None = None,
    atomic_defer: bool = False,
    arm: str = "c1_state",
    transition_action_source: str = "generated_spec",
    minimal_action_max_actions: int = MINIMAL_SMOKE_AGENT_MAX_ACTIONS,
    minimal_action_max_tokens: int = MINIMAL_SMOKE_AGENT_MAX_TOKENS,
    minimal_action_format_repairs: int = MINIMAL_SMOKE_AGENT_FORMAT_REPAIRS,
    minimal_action_cache: str | Path | None = None,
    minimal_action_planner_factory: Callable[..., MinimalSmokeActionAgent] | None = None,
    source_action_max_steps: int = SOURCE_TRANSITION_AGENT_MAX_STEPS,
    source_action_max_tokens: int = SOURCE_TRANSITION_AGENT_MAX_TOKENS,
    source_action_format_repairs: int = SOURCE_TRANSITION_AGENT_FORMAT_REPAIRS,
    source_action_cache: str | Path | None = None,
    source_action_planner_factory: Callable[..., SourceTransitionActionAgent]
    | None = None,
    pair_start_cache: str | Path | None = None,
    synthesis_agent_factory: Callable[[], Any] | None = None,
    synthesis_ablation: str = "full",
) -> dict[str, Any]:
    validate_variant(synthesis_ablation)
    if synthesis_ablation != "full" and (agent_kind != "llm" or not explore or arm != "c1_state"):
        raise ValueError("Synthesis ablations require c1_state with the LLM exploration protocol")
    if inner_workers < 1:
        raise ValueError("inner_workers must be at least 1")
    if transition_action_source not in STATE_TRANSITION_ACTION_SOURCES:
        raise ValueError(
            "transition_action_source must be generated_spec, minimal_agent_v1, "
            "or source_agent_v2"
        )
    if (
        transition_action_source in {"minimal_agent_v1", "source_agent_v2"}
        and arm != MINIMAL_STATE_ARM
    ):
        raise ValueError(
            f"{transition_action_source} requires arm='c1_state_minimal_agent'"
        )
    if transition_action_source == "generated_spec" and arm != "c1_state":
        raise ValueError("generated_spec transition source requires arm='c1_state'")
    missing_offline_planner = (
        transition_action_source == "minimal_agent_v1"
        and minimal_action_planner_factory is None
    ) or (
        transition_action_source == "source_agent_v2"
        and source_action_planner_factory is None
    )
    if agent_kind != "llm" and missing_offline_planner:
        raise ValueError(
            "diagnostic transition agents require agent_kind='llm' or an explicit "
            "fake planner factory"
        )
    serialized_unity_benchmarks = canonical_unity_serialized_benchmarks(
        unity_serialized=unity_serialized,
        unity_serialized_benchmarks=unity_serialized_benchmarks,
    )
    task_timeout_s = validate_task_timeout_s(task_timeout_s)
    base = Path(root).expanduser().resolve()
    out_dir = report_dir or new_report_dir()
    tasks = load_state_loop_tasks(base, tasks_path)
    requested_diagnostic_ids = (
        {str(task_id) for task_id in restore_diagnostic_task_ids}
        if restore_diagnostic_task_ids is not None
        else None
    )
    if requested_diagnostic_ids is not None and not restore_diagnostics:
        raise ValueError(
            "restore_diagnostic_task_ids requires restore_diagnostics=True"
        )
    known_task_ids = {task.id for task in tasks}
    unknown_diagnostic_ids = sorted((requested_diagnostic_ids or set()) - known_task_ids)
    if unknown_diagnostic_ids:
        raise ValueError(
            "unknown restore diagnostic task ids: " + ", ".join(unknown_diagnostic_ids)
        )
    diagnostic_task_ids = (
        (known_task_ids if requested_diagnostic_ids is None else requested_diagnostic_ids)
        if restore_diagnostics
        else set()
    )
    diagnostic_config = {
        "enabled": bool(restore_diagnostics),
        "task_ids": sorted(diagnostic_task_ids),
    }
    seeds = max(1, seeds)
    if seed is not None and seeds != 1:
        raise ValueError("seed selects one exact experiment seed and requires seeds=1")
    exact_seed = int(seed) if seed is not None else 0
    effective_model = model
    effective_base_url = base_url
    effective_api_key_env = api_key_env
    effective_headers: dict[str, str] | None = None
    provider_name = ""
    if agent_kind == "llm":
        effective_model, provider = resolve_model(
            model, default_base_url=base_url, default_api_key_env=api_key_env
        )
        effective_base_url = provider.base_url
        effective_api_key_env = provider.api_key_env
        effective_headers = provider.extra_headers
        provider_name = provider.name
    agent_label = "llm_explore" if (agent_kind == "llm" and explore) else agent_kind
    synthesis_cache_root = (
        Path(shared_synthesis_cache).expanduser().resolve()
        if shared_synthesis_cache is not None and agent_kind == "llm"
        else None
    )
    minimal_cache_root = (
        Path(minimal_action_cache).expanduser().resolve()
        if minimal_action_cache is not None
        and transition_action_source == "minimal_agent_v1"
        else None
    )
    source_cache_root = (
        Path(source_action_cache).expanduser().resolve()
        if source_action_cache is not None
        and transition_action_source == "source_agent_v2"
        else None
    )
    pair_start_cache_root = (
        Path(pair_start_cache).expanduser().resolve()
        if pair_start_cache is not None
        else None
    )
    pair_start_namespace = str(
        (run_meta or {}).get("p5_run_id")
        or (run_meta or {}).get("run_id")
        or invocation_id
    )

    def build_agent() -> Any:
        # A caller-supplied factory lets the replay-determinism experiment feed
        # frozen, already-synthesized checkpoints through the unmodified C1
        # execution and grading path instead of re-running synthesis.
        agent = (
            synthesis_agent_factory()
            if synthesis_agent_factory is not None
            else build_synthesis_agent(
                agent_kind,
                model=effective_model,
                base_url=effective_base_url,
                api_key_env=effective_api_key_env,
                temperature=temperature,
                max_tokens=max_tokens,
                explore=explore,
                max_steps=max_explore_steps,
                extra_headers=effective_headers,
                ablation_variant=synthesis_ablation,
            )
        )
        return CachedSynthesisAgent(
            agent,
            cache_root=synthesis_cache_root,
            base=base,
            seed=exact_seed,
            synth_model=effective_model,
            provider_name=provider_name,
            provider_base_url=effective_base_url,
            temperature=temperature,
            max_tokens=max_tokens,
            explore=explore,
            max_explore_steps=max_explore_steps,
        )

    def build_transition_planner() -> Any | None:
        if transition_action_source == "source_agent_v2":
            factory = source_action_planner_factory or SourceTransitionActionAgent
            return factory(
                model=effective_model,
                provider=provider_name,
                base_url=effective_base_url,
                api_key_env=effective_api_key_env,
                temperature=temperature,
                max_tokens=source_action_max_tokens,
                max_steps=source_action_max_steps,
                format_repairs=source_action_format_repairs,
                cache_root=source_cache_root,
                extra_headers=effective_headers,
            )
        if transition_action_source not in PLANNED_TRANSITION_ACTION_SOURCES:
            return None
        factory = minimal_action_planner_factory or MinimalSmokeActionAgent
        return factory(
            model=effective_model,
            provider=provider_name,
            base_url=effective_base_url,
            api_key_env=effective_api_key_env,
            temperature=temperature,
            max_tokens=minimal_action_max_tokens,
            max_actions=minimal_action_max_actions,
            format_repairs=minimal_action_format_repairs,
            cache_root=minimal_cache_root,
            extra_headers=effective_headers,
        )

    report_filename = (
        "state-minimal-agent.json"
        if transition_action_source in {"minimal_agent_v1", "source_agent_v2"}
        else "state-loop.json"
    )
    report_path = out_dir / report_filename
    existing: dict[str, dict[str, Any]] = {}
    prior_attempt_costs: dict[str, list[dict[str, Any]]] = {}
    if resume and report_path.exists() and seeds == 1:
        try:
            prior = json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as error:
            raise cost.UsageAccountingError(
                f"cannot safely resume state report {report_path}: {error}"
            ) from error
        if (
            prior.get("synthesis_model") in {effective_model, None}
            and prior.get("seed") == exact_seed
            and prior.get("arm", "c1_state") == arm
            and prior.get("transition_action_source", "generated_spec")
            == transition_action_source
        ):
            for item in prior.get("results", []):
                if not isinstance(item, dict) or not item.get("task_id"):
                    continue
                task_id = str(item["task_id"])
                incomplete = item.get("incomplete") is True
                attempts = _validated_attempt_ledger(
                    item, task_id=task_id, incomplete=incomplete
                )
                if not incomplete:
                    existing[task_id] = item
                else:
                    prior_attempt_costs[task_id] = attempts
    permanently_incomplete = sorted(
        task_id
        for task_id, attempts in prior_attempt_costs.items()
        if cost.contains_unknown_usage(attempts)
    )
    if permanently_incomplete:
        raise cost.UsageAccountingError(
            "refusing to resume state trials with permanently unknown provider usage; "
            "use a new report directory: " + ", ".join(permanently_incomplete)
        )
    nonretryable_incomplete = sorted(
        task_id
        for task_id, attempts in prior_attempt_costs.items()
        if attempts[-1].get("attempt_status") == "nonretryable_incomplete"
    )
    if nonretryable_incomplete:
        raise cost.UsageAccountingError(
            "refusing to replay nonretryable incomplete state trials; inspect the "
            "harness evidence and use a new report directory: "
            + ", ".join(nonretryable_incomplete)
        )
    results: list[dict[str, Any]] = []
    print(f"[state-loop] report_dir={out_dir} tasks={len(tasks)} agent={agent_label} seeds={seeds}", flush=True)

    def finalize_task_result(
        task: StateLoopTask, task_result: dict[str, Any]
    ) -> dict[str, Any]:
            task_result["synthesis_ablation"] = synthesis_ablation
            task_result["synthesis_ablation_revision"] = ABLATION_REVISION
            current_task_actual = dict(task_result.get("cost") or {})
            task_result["incomplete"] = _state_trial_incomplete(task_result)
            task_result["retryable_incomplete"] = (
                _state_trial_retryable_incomplete(task_result)
            )
            attempt_status = (
                "retryable_infrastructure"
                if task_result["incomplete"]
                and task_result["retryable_incomplete"]
                else "unknown_usage"
                if task_result["incomplete"]
                and cost.contains_unknown_usage(task_result)
                else "nonretryable_incomplete"
                if task_result["incomplete"]
                else "treatment"
            )
            retry_category, retry_reason = _state_retry_metadata(task_result)
            empty_cost = cost.CostMeter().summary()
            current_synthesis_accounting = {
                "attributed_cost": dict(
                    task_result.get("synthesis_attributed_cost") or empty_cost
                ),
                "actual_cost": dict(
                    task_result.get("synthesis_actual_cost") or empty_cost
                ),
                "transient_actual_cost": dict(
                    task_result.get("synthesis_transient_actual_cost")
                    or empty_cost
                ),
                "cache_hit": bool(task_result.get("synthesis_cache_hit")),
                "cache_fingerprint": task_result.get(
                    "synthesis_cache_fingerprint"
                ),
            }
            current_attempt = {
                # The attempt's headline cost is the comparison burden: what the
                # task spent plus the counterfactual synthesis attribution.
                **cost.merge_summaries(
                    [
                        current_task_actual,
                        current_synthesis_accounting["attributed_cost"],
                    ]
                ),
                "attempt_status": attempt_status,
                "retry_category": retry_category,
                "retry_reason": retry_reason,
                "task_actual_cost": current_task_actual,
                "synthesis_accounting": current_synthesis_accounting,
            }
            attempts = [
                *prior_attempt_costs.get(task.id, []),
                current_attempt,
            ]
            attempts, ledger_hash = _bind_attempt_ledger(
                attempts, task_id=task.id
            )
            task_actual_costs = [
                dict(attempt["task_actual_cost"]) for attempt in attempts
            ]
            synthesis_ledger = [
                dict(attempt.get("synthesis_accounting") or {})
                for attempt in attempts
            ]
            synthesis_attributed = cost.merge_summaries(
                [
                    dict(entry.get("attributed_cost") or empty_cost)
                    for entry in synthesis_ledger
                ]
            )
            synthesis_actual = cost.merge_summaries(
                [
                    dict(entry.get("actual_cost") or empty_cost)
                    for entry in synthesis_ledger
                ]
            )
            synthesis_transient = cost.merge_summaries(
                [
                    dict(entry.get("transient_actual_cost") or empty_cost)
                    for entry in synthesis_ledger
                ]
            )
            task_result["attempt_costs"] = attempts
            task_result["attempt_count"] = len(attempts)
            task_result["attempt_ledger_hash"] = ledger_hash
            task_result["synthesis_attributed_cost"] = synthesis_attributed
            task_result["synthesis_actual_cost"] = synthesis_actual
            task_result["synthesis_transient_actual_cost"] = synthesis_transient
            task_result["cost"] = cost.merge_summaries(
                [task_actual_costs[-1], synthesis_attributed]
            )
            task_result["actual_cost"] = cost.merge_summaries(
                [*task_actual_costs, synthesis_actual]
            )
            task_result["transient_infrastructure_cost"] = cost.merge_summaries(
                [*task_actual_costs[:-1], synthesis_transient]
            )
            return task_result

    scheduler: dict[str, Any] | None = None
    if seeds == 1:
        result_by_id: dict[str, dict[str, Any]] = dict(existing)
        result_lock = threading.RLock()
        legacy_lanes = build_lane_plan(
            tasks,
            workers=inner_workers,
            repo_for_task=lambda task: resolve_task_repo(base, task.issue),
        )
        actual_lane_plan = [
            [assignment.metadata() for assignment in lane] for lane in legacy_lanes
        ]
        if expected_lane_plan is not None and actual_lane_plan != expected_lane_plan:
            raise ValueError("C1 State inner lane plan differs from invocation provenance")
        canonical_ready_plan = build_ready_task_plan(
            tasks,
            repo_for_task=lambda task: resolve_task_repo(base, task.issue),
        )
        actual_task_plan = [item.metadata() for item in canonical_ready_plan]
        if expected_task_plan is not None and actual_task_plan != expected_task_plan:
            raise ValueError("C1 State inner task plan differs from invocation provenance")
        ready_plan = [
            item
            for item in canonical_ready_plan
            if item.task.id not in existing
        ]
        lane_count = min(inner_workers, max(1, len(ready_plan)))
        if ready_handle is None:
            scheduler_owner = ready_scheduler or C1ReadyScheduler(
                wait_timeout_s=runtime_lock_wait_timeout_s()
            )
            scheduler_invocation_id = retry_ready_invocation_id(
                invocation_id,
                ready_plan,
                {
                    task_id: len(attempts)
                    for task_id, attempts in prior_attempt_costs.items()
                },
            )
            ready_handle = scheduler_owner.register_invocation(
                scheduler_invocation_id,
                outer_ordinal=ready_outer_ordinal,
                registrations=ready_plan,
                resources_for_task=lambda task: c1_task_logical_resources(
                    task.benchmark,
                    unity_serialized_benchmarks=(
                        serialized_unity_benchmarks
                    ),
                ),
                worker_limit=lane_count,
            )
        task_by_id = {task.id: task for task in tasks}
        lifecycle = C1ReadyInvocationLifecycle(
            component="State",
            handle=ready_handle,
            requested_workers=inner_workers,
            fallback_workers=lane_count,
            atomic_defer=atomic_defer,
            serialized_unity_benchmarks=serialized_unity_benchmarks,
            global_stop_event=global_stop_event,
        )
        initial_lane_id, lane_ids = lifecycle.lane_ids(ready_ticket)

        def ordered_results() -> list[dict[str, Any]]:
            return [
                result_by_id[task.id]
                for task in tasks
                if task.id in result_by_id
            ]

        def run_lane(lane_id: int) -> None:
            agent = build_agent()
            transition_planner = build_transition_planner()
            initial_ticket = (
                ready_ticket if lane_id == initial_lane_id else None
            )
            try:
                while True:
                    if initial_ticket is not None:
                        ticket = initial_ticket
                        initial_ticket = None
                    elif atomic_defer:
                        ticket = None
                    else:
                        ticket = ready_handle.claim(lane_id=lane_id)
                    if ticket is None:
                        break
                    task = task_by_id[ticket.task_id]
                    registration = ticket.registration
                    print(
                        f"[state-loop] lane={lane_id} "
                        f"({registration.manifest_ordinal + 1}/{len(tasks)}) "
                        f"{task.benchmark}/{task.id}",
                        flush=True,
                    )
                    task_deadline_s = _resolve_task_timeout_s(task, task_timeout_s)
                    if ticket.queue_timed_out:
                        task_result = {
                            "task_id": task.id,
                            "benchmark": task.benchmark,
                            "state_description": task.state_description,
                            "seed": exact_seed,
                            "result": False,
                            "status": "incomplete",
                            "incomplete": True,
                            "infrastructure_error": True,
                            "runtime_lock_wait_timed_out": True,
                            "timed_out": False,
                            "task_timeout_s": task_deadline_s,
                            "reason": "C1 resource queue exceeded runtime_lock.wait_timeout_s",
                            "iterations": [],
                            "visual_grading_started": False,
                            "cost": cost.CostMeter().summary(),
                        }
                        lifecycle.complete_claimed(
                            ticket, status="queue_timeout"
                        )
                        task_result = finalize_task_result(task, task_result)
                        task_result["scheduler"] = {
                            **registration.metadata(),
                            **ticket.dispatch_metadata(),
                        }
                        task_result.update(ticket.queue_payload())
                        with result_lock:
                            result_by_id[task.id] = task_result
                        lifecycle.stop(
                            f"runtime resource queue timed out for task {task.id}"
                        )
                        break
                    sandbox_context = (
                        sandbox_manager.task_repo(
                            registration.source_repo,
                            invocation_id=invocation_id,
                            lane_id=lane_id,
                            task_id=task.id,
                            blocking=False,
                        )
                        if sandbox_manager is not None
                        else contextlib.nullcontext(None)
                    )
                    timing: dict[str, Any] | None = None
                    admitted = False

                    def on_resource_admitted() -> bool:
                        nonlocal timing, admitted
                        timing = lifecycle.admit(ticket, lane_id=lane_id)
                        if timing is None:
                            return False
                        admitted = True
                        return True

                    try:
                        with runtime_lock_wait_ledger():
                            with sandbox_context as sandbox:
                                if (
                                    not hasattr(_run_single_task, "__wrapped__")
                                    and not on_resource_admitted()
                                ):
                                    raise C1TaskDispatchCancelled(task.id)
                                task_result = _run_single_task(
                                    base,
                                    task,
                                    agent,
                                    out_dir=out_dir,
                                    max_iters=max_iters,
                                    headless=c1_task_headless(
                                        task.benchmark,
                                        requested_headless=headless,
                                        background_browser_headless=background_browser_headless,
                                    ),
                                    seed=exact_seed,
                                    restore_diagnostics=task.id in diagnostic_task_ids,
                                    source_repo=registration.source_repo,
                                    task_timeout_s=task_deadline_s,
                                    _unity_serialized=bool(
                                        serialized_unity_benchmarks
                                    ),
                                    _unity_serialized_benchmarks=(
                                        serialized_unity_benchmarks
                                    ),
                                    _nonblocking_resources=True,
                                    _on_resource_admitted=on_resource_admitted,
                                    minimal_planner=transition_planner,
                                    transition_action_source=transition_action_source,
                                    pair_start_cache=pair_start_cache_root,
                                    pair_start_id=_stable_pair_start_id(
                                        pair_start_namespace,
                                        effective_model,
                                        exact_seed,
                                        task.id,
                                    ),
                                    audit_model=effective_model,
                                )
                        completion = lifecycle.complete_task(ticket)
                        task_result = finalize_task_result(task, task_result)
                        if task_result.get("incomplete"):
                            if cost.contains_unknown_usage(task_result):
                                lifecycle.publish_global_unknown_stop()
                            lifecycle.stop(
                                f"incomplete task {task.id} stopped the invocation"
                            )
                        task_result["scheduler"] = {
                            **registration.metadata(),
                            **(timing or {}),
                            **completion,
                            **ticket.dispatch_metadata(),
                        }
                        task_result.update(ticket.queue_payload())
                        task_result["runtime_lock_wait_ms"] = ticket.queue_wait_ms()
                        task_result["sandbox"] = (
                            sandbox.metadata()
                            if sandbox is not None
                            else {"mode": "disabled"}
                        )
                    except RuntimeResourceBusy as error:
                        deferred_to_outer = lifecycle.defer_task(
                            ticket,
                            resource_kind=error.resource_kind,
                            resource_sha256=error.resource_sha256,
                        )
                        if deferred_to_outer:
                            raise C1TaskDeferred(task.id) from None
                        continue
                    except C1TaskDispatchCancelled:
                        if not lifecycle.cancel_task(ticket, task_id=task.id):
                            raise
                        break
                    except BaseException:  # noqa: BLE001
                        lifecycle.fail_task(ticket, admitted=admitted)
                        lifecycle.stop(f"task {task.id} raised an exception")
                        raise
                    with result_lock:
                        result_by_id[task.id] = task_result
                        current_results = ordered_results()
                        results[:] = current_results
                        current_scheduler = lifecycle.metadata()
                        _write_state_loop_incremental(
                            report_path,
                            results=current_results,
                            model=effective_model if agent_kind == "llm" else None,
                            seed=exact_seed,
                            complete=False,
                            run_meta=run_meta,
                            git_root=base,
                            synthesis_agent=agent_label,
                            restore_diagnostics=diagnostic_config,
                            task_timeout_s=task_timeout_s,
                            scheduler=current_scheduler,
                            arm=arm,
                            transition_action_source=transition_action_source,
                        )
                    if task_result.get("incomplete"):
                        print(
                            f"[state-loop] aborting invocation after incomplete task {task.id}; "
                            "not-yet-started tasks are intentionally not charged",
                            flush=True,
                        )
                        break
            finally:
                close = getattr(agent, "close", None)
                if callable(close):
                    close()
                planner_close = getattr(transition_planner, "close", None)
                if callable(planner_close):
                    planner_close()

        failures: list[BaseException] = []
        with ThreadPoolExecutor(max_workers=len(lane_ids)) as pool:
            futures = [pool.submit(run_lane, lane_id) for lane_id in lane_ids]
            for future in futures:
                try:
                    future.result()
                except BaseException as error:  # noqa: BLE001
                    failures.append(error)
        if failures:
            raise failures[0]
        results = ordered_results()
        scheduler = lifecycle.metadata()
    else:
        if inner_workers != 1:
            raise ValueError("inner task workers require an exact single experiment seed")
        agent = build_agent()
        transition_planner = build_transition_planner()
        try:
            for position, task in enumerate(tasks, start=1):
                if global_stop_event is not None and global_stop_event.is_set():
                    break
                print(
                    f"[state-loop] ({position}/{len(tasks)}) {task.benchmark}/{task.id}",
                    flush=True,
                )
                seed_results = [
                    _run_single_task(
                        base,
                        task,
                        agent,
                        out_dir=out_dir / f"seed{sample_seed}",
                        max_iters=max_iters,
                        headless=headless,
                        seed=sample_seed,
                        restore_diagnostics=task.id in diagnostic_task_ids,
                        task_timeout_s=_resolve_task_timeout_s(task, task_timeout_s),
                        minimal_planner=transition_planner,
                        transition_action_source=transition_action_source,
                        pair_start_cache=pair_start_cache_root,
                        pair_start_id=_stable_pair_start_id(
                            pair_start_namespace,
                            effective_model,
                            sample_seed,
                            task.id,
                        ),
                        audit_model=effective_model,
                    )
                    for sample_seed in range(seeds)
                ]
                aggregate = _aggregate_seed_results(task, seed_results)
                aggregate["incomplete"] = any(
                    _state_trial_incomplete(item) for item in seed_results
                )
                aggregate["retryable_incomplete"] = all(
                    _state_trial_retryable_incomplete(item)
                    for item in seed_results
                    if _state_trial_incomplete(item)
                )
                results.append(aggregate)
        finally:
            close = getattr(agent, "close", None)
            if callable(close):
                close()
            planner_close = getattr(transition_planner, "close", None)
            if callable(planner_close):
                planner_close()

    payload = {
        **(
            {
                "schema": "c1-state-minimal-agent-v1",
                "schema_version": 1,
                "formal": False,
                "excluded_from_formal": True,
            }
            if transition_action_source == "minimal_agent_v1"
            else {
                "schema": "c1-state-source-agent-v2",
                "schema_version": 2,
                "formal": False,
                "excluded_from_formal": True,
            }
            if transition_action_source == "source_agent_v2"
            else {"schema_version": 4}
        ),
        "state_stage_metrics": {
            "schema_version": STATE_STAGE_METRICS_SCHEMA_VERSION,
            "start_gate_boundary": "before_registered_transition",
            "start_success": "before_semantic_and_before_visual",
            "goal_success": "after_semantic_and_after_visual",
            "e2e_success": "start_success_and_goal_success",
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "experiment": (
            "state_loop_minimal_action_agent"
            if transition_action_source == "minimal_agent_v1"
            else "state_loop_source_transition_agent"
            if transition_action_source == "source_agent_v2"
            else "state_loop"
        ),
        "arm": arm,
        **(
            {"transition_action_source": transition_action_source}
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else {}
        ),
        "synthesis_agent": agent_label,
        "synthesis_model": effective_model if agent_kind == "llm" else None,
        "max_iters": max_iters,
        "seeds": seeds,
        "seed": exact_seed if seeds == 1 else None,
        "restore_diagnostics": diagnostic_config,
        "task_timeout_s": task_timeout_s,
        "scheduler": scheduler,
        "claim_boundary": (
            "Preconditions, reference steps, and source are method inputs; the hidden "
            "oracle_spec is used only by the grader to generate before/after golden "
            "screenshots."
        ),
        "task_file": str((base / tasks_path).resolve() if not Path(tasks_path).is_absolute() else tasks_path),
        "results": results,
        "summary": _summarize(results, seeds=seeds),
        "complete": len(results) == len(tasks) and all(not item.get("incomplete") for item in results),
    }
    (
        payload["retryable_incomplete"],
        payload["incomplete_category"],
    ) = _state_report_incomplete_contract(results)
    experiment_id = (
        "state_loop_minimal_action_agent"
        if transition_action_source == "minimal_agent_v1"
        else "state_loop_source_transition_agent"
        if transition_action_source == "source_agent_v2"
        else "state_loop"
    )
    run_context.stamp_payload(
        payload, run_meta=run_meta, experiment_id=experiment_id, git_root=base
    )
    _atomic_state_json(report_path, payload)
    markdown_path = out_dir / (
        "state-minimal-agent.md"
        if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
        else "state-loop.md"
    )
    markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
    return {**payload, "report_dir": str(out_dir)}


def _state_trial_incomplete(result: dict[str, Any]) -> bool:
    if result.get("incomplete") is True:
        return True
    if any(
        isinstance(item, dict) and item.get("incomplete") is True
        for item in (result.get("iterations") or [])
    ):
        return True
    status = str(result.get("status", ""))
    reason = str(result.get("reason", "")).lower()
    result_cost = result.get("cost")
    if isinstance(result_cost, dict) and result_cost.get("usage_complete") is not True:
        return True
    if status in {"unsupported"}:
        return True
    if any(
        marker in reason
        for marker in (
            "task setup failed",
            "session bootstrap failed",
            "oracle golden generation failed",
            "runtime failed",
            "gateway unavailable",
            "rate limit",
            "quota",
            "connection error",
            "timed out",
        )
    ):
        return True
    verdict = result.get("pair_verdict")
    if isinstance(verdict, dict):
        halves = [verdict.get("before"), verdict.get("after")]
        if any(isinstance(half, dict) and half.get("label") == "uncertain" for half in halves):
            return True
    return False


def _state_visual_judge_infrastructure(
    result: dict[str, Any],
) -> tuple[str, str] | None:
    """Return a typed transient failure from a visual-judge half, if present.

    Replaying after an ordinary visual verdict would resample a treatment whose
    runtime has already reached grading, so the default remains nonretryable.
    A judge transport/rate-limit failure is different: no visual decision was
    produced.  Recognizing only the explicit fail-closed ``judge_failure`` plus
    an allow-listed error category keeps malformed or contradictory verdicts
    nonretryable while allowing clean-root recovery from provider outages.
    """

    candidates = [result]
    candidates.extend(
        item
        for item in (result.get("iterations") or [])
        if isinstance(item, dict)
    )
    for candidate in candidates:
        verdict = candidate.get("pair_verdict")
        if not isinstance(verdict, dict):
            continue
        for stage in ("before", "after"):
            half = verdict.get(stage)
            if not isinstance(half, dict) or half.get("judge_failure") is not True:
                continue
            detail = str(
                half.get("judge_error")
                or half.get("rationale")
                or "visual judge provider failure"
            ).strip()
            category = run_context.error_category(RuntimeError(detail))
            if category in {"infra", "rate_limit"}:
                return category, run_context.safe_error_text(detail, limit=450)
    return None


def _state_trial_retryable_incomplete(result: dict[str, Any]) -> bool:
    if not _state_trial_incomplete(result) or cost.contains_unknown_usage(result):
        return False
    if _state_visual_judge_infrastructure(result) is not None:
        return True
    if (
        result.get("start_gate_status") == "pair_start_attestation_incomplete"
        and result.get("infrastructure_error") is True
    ):
        return True
    # Once a stochastic candidate reached visual grading, restarting the whole
    # trial could select a different outcome. Judge/evidence failures therefore
    # pause for diagnosis instead of being auto-replayed. Explicit visual-judge
    # transport failures were handled above because they produced no decision.
    if result.get("visual_grading_started") is True:
        return False
    if result.get("infrastructure_error") is True:
        return True
    reasons = [str(result.get("reason") or "")]
    reasons.extend(
        str(item.get("reason") or "")
        for item in (result.get("iterations") or [])
        if isinstance(item, dict) and item.get("incomplete") is True
    )
    return any(
        run_context.error_category(RuntimeError(reason)) in {"infra", "rate_limit"}
        for reason in reasons
        if reason
    )


def _state_retry_metadata(result: dict[str, Any]) -> tuple[str | None, str | None]:
    """Return the first allow-listed, scrubbed outage reason for this attempt."""

    if result.get("retryable_incomplete") is not True:
        return None, None
    judge_failure = _state_visual_judge_infrastructure(result)
    if judge_failure is not None:
        category, reason = judge_failure
        marker = "service unavailable" if category == "infra" else "rate limit"
        return category, f"{marker}: visual judge: {reason}"[:500]
    reasons = [str(result.get("reason") or "")]
    reasons.extend(
        str(item.get("reason") or "")
        for item in (result.get("iterations") or [])
        if isinstance(item, dict) and item.get("incomplete") is True
    )
    if result.get("infrastructure_error") is True:
        reason = next((item for item in reasons if item), "typed runtime failure")
        scrubbed = run_context.safe_error_text(reason, limit=450)
        return "infra", f"service unavailable: {scrubbed}"[:500]
    for reason in reasons:
        category = run_context.error_category(RuntimeError(reason))
        if category in {"infra", "rate_limit"}:
            marker = "service unavailable" if category == "infra" else "rate limit"
            scrubbed = run_context.safe_error_text(reason, limit=450)
            return category, f"{marker}: {scrubbed}"[:500]
    return None, None


def _state_report_incomplete_contract(
    results: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    incomplete = [item for item in results if item.get("incomplete") is True]
    if not incomplete:
        return False, None
    if cost.contains_unknown_usage(incomplete):
        return False, "permanent_unknown_usage"
    if all(item.get("retryable_incomplete") is True for item in incomplete):
        return True, "transient_infrastructure"
    return False, "nonretryable_incomplete_evidence"


def _atomic_state_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _write_state_loop_incremental(
    path: Path,
    *,
    results: list[dict[str, Any]],
    model: str | None,
    seed: int,
    complete: bool,
    run_meta: dict[str, Any] | None,
    git_root: Path,
    synthesis_agent: str,
    restore_diagnostics: dict[str, Any],
    task_timeout_s: float,
    scheduler: dict[str, Any] | None = None,
    arm: str = "c1_state",
    transition_action_source: str = "generated_spec",
) -> None:
    retryable_incomplete, incomplete_category = (
        _state_report_incomplete_contract(results)
    )
    payload = {
        **(
            {
                "schema": "c1-state-minimal-agent-v1",
                "schema_version": 1,
                "formal": False,
                "excluded_from_formal": True,
            }
            if transition_action_source == "minimal_agent_v1"
            else {
                "schema": "c1-state-source-agent-v2",
                "schema_version": 2,
                "formal": False,
                "excluded_from_formal": True,
            }
            if transition_action_source == "source_agent_v2"
            else {"schema_version": 4}
        ),
        "state_stage_metrics": {
            "schema_version": STATE_STAGE_METRICS_SCHEMA_VERSION,
            "start_gate_boundary": "before_registered_transition",
            "start_success": "before_semantic_and_before_visual",
            "goal_success": "after_semantic_and_after_visual",
            "e2e_success": "start_success_and_goal_success",
        },
        "experiment": (
            "state_loop_minimal_action_agent"
            if transition_action_source == "minimal_agent_v1"
            else "state_loop_source_transition_agent"
            if transition_action_source == "source_agent_v2"
            else "state_loop"
        ),
        "arm": arm,
        **(
            {"transition_action_source": transition_action_source}
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else {}
        ),
        "synthesis_agent": synthesis_agent,
        "synthesis_model": model,
        "seed": seed,
        "restore_diagnostics": restore_diagnostics,
        "task_timeout_s": task_timeout_s,
        "scheduler": scheduler,
        "results": results,
        "summary": _summarize(results, seeds=1),
        "complete": bool(complete),
        "retryable_incomplete": retryable_incomplete,
        "incomplete_category": incomplete_category,
    }
    run_context.stamp_payload(
        payload,
        run_meta=run_meta,
        experiment_id=(
            "state_loop_minimal_action_agent"
            if transition_action_source == "minimal_agent_v1"
            else "state_loop_source_transition_agent"
            if transition_action_source == "source_agent_v2"
            else "state_loop"
        ),
        git_root=git_root,
    )
    _atomic_state_json(path, payload)


def _leased_state_task(function):
    """Acquire backend resources before starting one task execution deadline."""

    @functools.wraps(function)
    def wrapped(base: Path, task: StateLoopTask, *args: Any, **kwargs: Any):
        function_kwargs = dict(kwargs)
        task_id = str(getattr(task, "id", "c1-task"))
        benchmark = str(getattr(task, "benchmark", ""))
        unity_serialized = bool(
            function_kwargs.pop("_unity_serialized", False)
        )
        unity_serialized_benchmarks = function_kwargs.pop(
            "_unity_serialized_benchmarks", None
        )
        nonblocking_resources = bool(
            function_kwargs.pop("_nonblocking_resources", False)
        )
        on_resource_admitted = function_kwargs.pop(
            "_on_resource_admitted", None
        )
        task_timeout_s = validate_task_timeout_s(
            function_kwargs.get("task_timeout_s", DEFAULT_C1_TASK_TIMEOUT_S)
        )
        with runtime_lock_wait_ledger() as lock_waits:
            try:
                try:
                    repo = resolve_task_repo(base, task.issue)
                except Exception:
                    # Let the producer retain its normal structured
                    # setup-failure row.
                    with c1_task_deadline(task_id, task_timeout_s):
                        result = function(
                            base,
                            task,
                            *args,
                            _deadline_active=True,
                            **function_kwargs,
                        )
                else:
                    with c1_task_resource_locks(
                        repo,
                        benchmark,
                        unity_serialized=unity_serialized,
                        unity_serialized_benchmarks=(
                            unity_serialized_benchmarks
                        ),
                        blocking=not nonblocking_resources,
                    ):
                        if callable(on_resource_admitted):
                            if on_resource_admitted() is False:
                                raise C1TaskDispatchCancelled(task_id)
                        with c1_task_deadline(task_id, task_timeout_s):
                            result = function(
                                base,
                                task,
                                *args,
                                _deadline_active=True,
                                **function_kwargs,
                            )
            except RuntimeLockWaitTimeout as error:
                result = {
                    "task_id": task_id,
                    "benchmark": benchmark,
                    "state_description": getattr(
                        task, "state_description", ""
                    ),
                    "preconditions": getattr(
                        task, "preconditions", ""
                    ),
                    "reference_steps": getattr(
                        task, "reference_steps", ""
                    ),
                    **(
                        {
                            "goal_description": str(
                                getattr(task, "goal_description", "") or ""
                            )
                        }
                        if str(getattr(task, "goal_description", "") or "").strip()
                        else {}
                    ),
                    "prompt_revision": str(
                        getattr(task, "prompt_revision", "") or ""
                    ),
                    "horizon_class": getattr(task, "horizon_class", ""),
                    "oracle_spec": getattr(task, "oracle_spec", ""),
                    "seed": int(function_kwargs.get("seed") or 0),
                    "result": False,
                    "status": "incomplete",
                    "incomplete": True,
                    "infrastructure_error": True,
                    "runtime_lock_wait_timed_out": True,
                    "timed_out": False,
                    "task_timeout_s": task_timeout_s,
                    "reason": str(error),
                    "iterations": [],
                    "visual_grading_started": False,
                    "restore_diagnostics_enabled": bool(
                        function_kwargs.get("restore_diagnostics", True)
                    ),
                    "cost": cost.CostMeter().summary(),
                }
            if "state_stage_metrics_schema_version" not in result:
                terminal = result.get("result") is True
                initialize_state_stage_metrics(result)
                result["result"] = terminal
                result["e2e_success"] = terminal
            result.update(runtime_lock_wait_payload(lock_waits))
        result.setdefault("runtime_lock_wait_timed_out", False)
        result.setdefault("task_timeout_s", task_timeout_s)
        _bind_task_random_control(result, task)
        return result

    return wrapped


@_leased_state_task
def _run_single_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    *,
    out_dir: Path,
    max_iters: int,
    headless: bool,
    seed: int = 0,
    restore_diagnostics: bool = True,
    source_repo: Path | None = None,
    minimal_planner: Any | None = None,
    transition_action_source: str = "generated_spec",
    pair_start_cache: Path | None = None,
    pair_start_id: str | None = None,
    audit_model: str | None = None,
    task_timeout_s: float = DEFAULT_C1_TASK_TIMEOUT_S,
    _deadline_active: bool = False,
) -> dict[str, Any]:
    # Per-task cost ledger: every LLM call (synthesis/ReAct/repair) made while this
    # meter is active is attributed to the task, for the C1 cost-accuracy claim.
    # Checkpoint synthesis meters itself separately so a cache hit can report zero
    # actual spend beside the full counterfactual burden.
    if isinstance(agent, CachedSynthesisAgent):
        agent.bind_task(task)
    trial_wall_started = time.perf_counter()
    with cost.scoped_meter() as task_meter, ablation_scope(getattr(agent, "ablation_variant", "full")):
        try:
            deadline_context = (
                contextlib.nullcontext()
                if _deadline_active
                else c1_task_deadline(task.id, task_timeout_s)
            )
            with deadline_context:
                raise_if_task_timed_out()
                result = _run_single_task_body(
                    base,
                    task,
                    agent,
                    out_dir=out_dir,
                    max_iters=max_iters,
                    headless=headless,
                    seed=seed,
                    restore_diagnostics=restore_diagnostics,
                    source_repo=source_repo,
                    minimal_planner=minimal_planner,
                    transition_action_source=transition_action_source,
                    pair_start_cache=pair_start_cache,
                )
                raise_if_task_timed_out()
        except C1TaskTimeout as error:
            result = {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "state_description": task.state_description,
                "preconditions": task.preconditions,
                "reference_steps": task.reference_steps,
                **(
                    {"goal_description": task.goal_description}
                    if task.goal_description.strip()
                    else {}
                ),
                "prompt_revision": str(
                    getattr(task, "prompt_revision", "") or ""
                ),
                "horizon_class": task.horizon_class,
                "oracle_spec": task.oracle_spec,
                "seed": seed,
                "result": False,
                "status": "incomplete",
                "incomplete": True,
                "infrastructure_error": False,
                "timed_out": True,
                "task_timeout_s": float(task_timeout_s),
                "reason": str(error),
                "iterations": [],
                "visual_grading_started": False,
            }
        if "state_stage_metrics_schema_version" not in result:
            # Covers timeout/error rows built outside ``_run_single_task_body``.
            terminal = result.get("result") is True
            initialize_state_stage_metrics(result)
            result["result"] = terminal
            result["e2e_success"] = terminal
        if "transition_action_source" not in result:
            _initialize_minimal_smoke_result(
                result, transition_action_source=transition_action_source
            )
    result.pop("_pair_start_cache", None)
    for iteration_result in result.get("iterations") or []:
        if isinstance(iteration_result, dict):
            iteration_result.pop("_pair_start_cache", None)
    result["restore_diagnostics_enabled"] = bool(restore_diagnostics)
    if pair_start_id:
        result["accepted_start_id"] = pair_start_id
        result["pair_start_id"] = pair_start_id
    if audit_model:
        result["model"] = audit_model
    result.setdefault("task_timeout_s", float(task_timeout_s))
    result["cost"] = task_meter.summary()
    result["trial_wall_clock_ms"] = int((time.perf_counter() - trial_wall_started) * 1000)
    if transition_action_source in {"minimal_agent_v1", "source_agent_v2"}:
        for call in result["cost"].get("calls", []):
            if isinstance(call, dict) and call.get("kind") in {
                "smoke_action_planning",
                "smoke_action_format_repair",
            }:
                call.pop("messages", None)
                call.pop("response", None)
    minimal_report = result.get("minimal_smoke_agent")
    if isinstance(minimal_report, dict):
        result["minimal_action_actual_cost"] = dict(
            minimal_report.get("cost") or cost.CostMeter().summary()
        )
        result["minimal_action_attributed_cost"] = dict(
            minimal_report.get("attributed_cost") or cost.CostMeter().summary()
        )
    source_report = result.get("source_transition_agent")
    if isinstance(source_report, dict):
        result["source_action_actual_cost"] = dict(
            source_report.get("cost") or cost.CostMeter().summary()
        )
        result["source_action_attributed_cost"] = dict(
            source_report.get("attributed_cost") or cost.CostMeter().summary()
        )
    _apply_state_synthesis_accounting(result, agent)
    if (
        pair_start_cache is not None
        and not isinstance(result.get("pair_start_gate"), dict)
        and result.get("incomplete") is not True
        and result.get("generated_spec_hash") is None
        and isinstance(result.get("synthesis_cache_fingerprint"), str)
    ):
        _freeze_pair_start_construction_failure(
            result,
            task=task,
            pair_start_cache=pair_start_cache,
        )
    # The body measures the method path at the exact synthesis/restore/action
    # boundaries. Never reconstruct C1 time from whole-task wall time: that would
    # charge cold diagnostics, oracle-golden generation, and hidden grading to E.
    _set_state_method_timing(result)
    _finalize_minimal_smoke_verdict(result)
    _stamp_minimal_core_audit(result)
    return result


def _restore_path_ms(timing: Any, *, prefer_authoritative_warm: bool) -> int:
    if not isinstance(timing, dict):
        return 0
    if prefer_authoritative_warm:
        ordered = ("warm_restore_ms", "fast_restore_ms")
    elif timing.get("fast_restore_used"):
        ordered = ("fast_restore_ms", "warm_restore_ms")
    else:
        ordered = ("warm_restore_ms", "fast_restore_ms")
    for field_name in ordered:
        value = timing.get(field_name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return max(0, int(value))
    return 0


def _set_state_method_timing(
    result: dict[str, Any],
    *,
    checkpoint_synthesis_ms: int | None = None,
    time_to_start_ms: int | None = None,
    time_start_to_goal_ms: int | None = None,
) -> None:
    """Stamp E timing from method-only wall-time components.

    The authoritative full warm restore is the method's start construction. The
    operational after path may use the validated fast snapshot. Diagnostic cold
    restores and the extra fast-vs-full probe are intentionally not summed.
    """

    synth_ms = max(
        0,
        int(
            result.get("checkpoint_synthesis_ms", 0)
            if checkpoint_synthesis_ms is None
            else checkpoint_synthesis_ms
        ),
    )
    start_ms = (
        _restore_path_ms(result.get("restore_timing"), prefer_authoritative_warm=True)
        if time_to_start_ms is None
        else max(0, int(time_to_start_ms))
    )
    action_execution_ms = (
        _restore_path_ms(result.get("after_timing"), prefer_authoritative_warm=False)
        if time_start_to_goal_ms is None
        else max(0, int(time_start_to_goal_ms))
    )
    planning_ms = (
        max(0, int(result.get("smoke_action_generation_ms", 0) or 0))
        if result.get("transition_action_source")
        in {"minimal_agent_v1", "source_agent_v2"}
        else 0
    )
    goal_ms = action_execution_ms + planning_ms
    excluding_ms = start_ms + goal_ms
    including_ms = synth_ms + excluding_ms
    result["checkpoint_synthesis_ms"] = synth_ms
    result["checkpoint_restore_ms"] = start_ms
    result["time_to_start_ms"] = start_ms
    result["time_start_to_goal_ms"] = goal_ms
    if result.get("transition_action_source") in {
        "minimal_agent_v1",
        "source_agent_v2",
    }:
        result["smoke_action_execution_ms"] = action_execution_ms
    result["time_e2e_excluding_synthesis_ms"] = excluding_ms
    result["time_e2e_including_synthesis_ms"] = including_ms
    result["termination_time_ms"] = including_ms
    result.setdefault("runtime_bootstrap_ms", int(result.get("session_bootstrap_ms", 0) or 0))
    result.setdefault("grader_ms", 0)


def _initialize_minimal_smoke_result(
    result: dict[str, Any], *, transition_action_source: str
) -> None:
    if transition_action_source not in {"minimal_agent_v1", "source_agent_v2"}:
        return
    result["transition_action_source"] = transition_action_source
    planner_field = (
        "source_transition_agent"
        if transition_action_source == "source_agent_v2"
        else "minimal_smoke_agent"
    )
    result.update(
        original_generated_actions_ignored=True,
        original_generated_actions_sha256=None,
        original_generated_assertions_sha256=None,
        planner_called=False,
        planner_called_after_start=None,
        action_execution_started=False,
        smoke_action_verdict="not_evaluated_start_failed",
        smoke_action_failure_stage="start",
        smoke_action_generation_ms=0,
        smoke_action_execution_ms=0,
    )
    result[planner_field] = {
        "schema_version": 2 if transition_action_source == "source_agent_v2" else 1,
        "status": "not_called",
    }


def _mark_transition_planner_called(result: dict[str, Any]) -> None:
    """Record the planner call at its real post-Start execution boundary."""

    if result.get("transition_action_source") not in PLANNED_TRANSITION_ACTION_SOURCES:
        return
    result["planner_called"] = True
    result["planner_called_after_start"] = bool(
        result.get("start_success") is True
        and result.get("start_grading_started") is True
    )


def _mark_transition_action_execution_started(result: dict[str, Any]) -> None:
    """Record entry into the runtime transition rather than infer it later."""

    result["action_execution_started"] = True


def _plan_minimal_smoke_transition(
    result: dict[str, Any],
    *,
    planner: Any | None,
    base: Path,
    task: StateLoopTask,
    generated_spec: dict[str, Any],
    task_dir: Path,
    seed: int,
) -> dict[str, Any] | None:
    """Plan one post-Start transition or finish the diagnostic trial fail-closed."""

    source = result.get("transition_action_source")
    if source not in {"minimal_agent_v1", "source_agent_v2"}:
        return copy.deepcopy(generated_spec)
    if planner is None:
        raise RuntimeError(f"{source} requires a lane-local transition planner")
    original_actions = generated_spec.get("actions")
    original_assertions = generated_spec.get("assertions")
    result["original_generated_actions_sha256"] = _sha256_json(
        original_actions if isinstance(original_actions, list) else []
    )
    result["original_generated_assertions_sha256"] = _sha256_json(
        original_assertions if isinstance(original_assertions, list) else []
    )
    _mark_transition_planner_called(result)
    try:
        if source == "source_agent_v2":
            plan = planner.plan(
                task,
                generated_spec,
                base=base,
                seed=seed,
                artifact_dir=task_dir / "source-transition-agent",
                pair_start_fingerprint=result.get("pair_start_fingerprint"),
            )
        else:
            plan = planner.plan(
                task,
                generated_spec,
                seed=seed,
                artifact_dir=task_dir / "minimal-smoke-agent",
            )
    except Exception as error:
        infrastructure = run_context.error_category(error) in {"infra", "rate_limit"}
        result.update(
            **{
                (
                    "source_transition_agent"
                    if source == "source_agent_v2"
                    else "minimal_smoke_agent"
                ): {
                    "schema_version": 2 if source == "source_agent_v2" else 1,
                "status": "incomplete" if infrastructure else "invalid",
                "errors": [f"{type(error).__name__}: {error}"],
                }
            },
            smoke_action_verdict="incomplete" if infrastructure else "not_pass",
            smoke_action_failure_stage=(
                "infrastructure" if infrastructure else "generation"
            ),
            smoke_action_generation_ms=0,
            status="incomplete" if infrastructure else "failure",
            incomplete=infrastructure,
            infrastructure_error=infrastructure,
            reason=f"transition action planning failed: {error}",
            goal_evaluation_status="not_evaluated_action_generation_failed",
            after_semantic_status="not_evaluated",
            after_visual_status="not_evaluated",
            after_semantic_ok=False,
            after_visual_ok=False,
            goal_success=False,
            e2e_success=False,
            result=False,
        )
        return None
    planner_field = (
        "source_transition_agent"
        if source == "source_agent_v2"
        else "minimal_smoke_agent"
    )
    result[planner_field] = plan.to_report()
    result["smoke_action_generation_ms"] = int(plan.generation_ms)
    if not plan.usable:
        incomplete = plan.status == "incomplete"
        result.update(
            smoke_action_verdict="incomplete" if incomplete else "not_pass",
            smoke_action_failure_stage=(
                "infrastructure" if incomplete else "contract"
            ),
            status="incomplete" if incomplete else "failure",
            incomplete=incomplete,
            infrastructure_error=incomplete,
            reason=(
                "transition action planning evidence is incomplete"
                if incomplete
                else "transition action plan violates the public executable contract"
            ),
            goal_evaluation_status="not_evaluated_action_generation_failed",
            after_semantic_status="not_evaluated",
            after_visual_status="not_evaluated",
            after_semantic_ok=False,
            after_visual_ok=False,
            goal_success=False,
            e2e_success=False,
            result=False,
        )
        return None
    transition_spec = copy.deepcopy(generated_spec)
    transition_spec["actions"] = [copy.deepcopy(action) for action in plan.actions]
    # Planner assertions are an auxiliary generation target only. Runtime
    # backends receive the registered hidden assertions through their existing
    # oracle path, never these model-authored assertions.
    transition_spec["assertions"] = []
    transition_path = task_dir / (
        "source-transition-spec.yaml"
        if source == "source_agent_v2"
        else "minimal-transition-spec.yaml"
    )
    transition_path.write_text(
        yaml.safe_dump(transition_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result.update(
        smoke_action_verdict="not_pass",
        smoke_action_failure_stage="execution",
        **{
            (
                "source_transition_spec_path"
                if source == "source_agent_v2"
                else "minimal_transition_spec_path"
            ): str(transition_path)
        },
    )
    return transition_spec


def _finalize_minimal_smoke_verdict(result: dict[str, Any]) -> None:
    source = result.get("transition_action_source")
    if source not in {"minimal_agent_v1", "source_agent_v2"}:
        return
    planner = result.get(
        "source_transition_agent"
        if source == "source_agent_v2"
        else "minimal_smoke_agent"
    )
    planner_status = planner.get("status") if isinstance(planner, dict) else None
    if result.get("incomplete") is True:
        result["smoke_action_verdict"] = "incomplete"
        result["smoke_action_failure_stage"] = "infrastructure"
        return
    if result.get("start_success") is not True:
        result["smoke_action_verdict"] = "not_evaluated_start_failed"
        result["smoke_action_failure_stage"] = "start"
        return
    if planner_status not in {"generated", "repaired"}:
        result.setdefault("smoke_action_verdict", "not_pass")
        result.setdefault("smoke_action_failure_stage", "contract")
        return
    if result.get("goal_success") is True and result.get("e2e_success") is True:
        result["smoke_action_verdict"] = "pass"
        result["smoke_action_failure_stage"] = None
    elif result.get("after_semantic_ok") is not True:
        result["smoke_action_verdict"] = "not_pass"
        result["smoke_action_failure_stage"] = _minimal_smoke_failure_stage(result)
    elif result.get("after_visual_ok") is not True:
        result["smoke_action_verdict"] = "not_pass"
        result["smoke_action_failure_stage"] = "visual"
    else:
        result["smoke_action_verdict"] = "not_pass"
        result["smoke_action_failure_stage"] = "execution"


def _stamp_minimal_core_audit(result: dict[str, Any]) -> None:
    """Record treatment-boundary facts without making artifact SHA authoritative."""

    planner_called = result.get("planner_called") is True
    raw_planner_called_after_start = result.get("planner_called_after_start")
    planner_called_after_start = (
        raw_planner_called_after_start
        if isinstance(raw_planner_called_after_start, bool)
        else None
    )
    action_execution_started = result.get("action_execution_started") is True
    usage_complete = result.get("cost", {}).get("usage_complete") is True
    result.update(
        planner_called=planner_called,
        planner_called_after_start=planner_called_after_start,
        action_execution_started=action_execution_started,
        usage_complete=usage_complete,
        minimal_core_audit={
            "schema": "c1-minimal-core-audit-v1",
            "task_id": result.get("task_id"),
            "model": result.get("model") or result.get("synthesis_model"),
            "seed": result.get("seed"),
            "accepted_start_id": result.get("accepted_start_id"),
            "before_semantic_ok": result.get("before_semantic_ok"),
            "before_visual_ok": result.get("before_visual_ok"),
            "start_success": result.get("start_success"),
            "planner_called": planner_called,
            "planner_called_after_start": planner_called_after_start,
            "action_execution_started": action_execution_started,
            "goal_success": result.get("goal_success"),
            "e2e_success": result.get("e2e_success"),
            "incomplete": result.get("incomplete") is True,
            "usage_complete": usage_complete,
        },
    )


def _minimal_smoke_failure_stage(result: Mapping[str, Any]) -> str:
    if result.get("after_failure_phase") == "smoke_execution":
        return "execution"
    if result.get("after_failure_phase") == "oracle_result":
        return "semantic"
    runtime_report = result.get("runtime_report")
    if isinstance(runtime_report, Mapping) and runtime_report.get("failure_phase") in {
        "action_generation",
        "registered_transition",
        "transition",
        "smoke_execution",
    }:
        return "execution"
    reason = str(result.get("reason") or "").lower()
    if any(
        marker in reason
        for marker in (
            "after-state smoke failed",
            "registered transition",
            "action execution",
            "transition failed",
        )
    ):
        return "execution"
    return "semantic"


def _normalize_state_before_verdict(verdict: Any) -> dict[str, Any]:
    """Normalize staged Start output, including minimal legacy test doubles."""

    payload = dict(verdict) if isinstance(verdict, dict) else {}
    before = payload.get("before")
    if not isinstance(before, dict):
        terminal = payload.get("result") is True
        before = {
            "stage": "before",
            "label": "correct" if terminal else "incorrect",
            "compatibility_fallback": True,
        }
    before_ok = before.get("label") == "correct"
    payload.update(
        {
            "result": False,
            "status": "pending" if before_ok else "failure",
            "before": before,
            "after": {
                "stage": "after",
                "label": "not_evaluated",
                "evaluation_status": (
                    "pending_phase_b"
                    if before_ok
                    else "not_evaluated_before_failed"
                ),
            },
            "after_evaluated": False,
        }
    )
    return payload


def _normalize_state_goal_verdict(
    verdict: Any, *, prior_before: dict[str, Any]
) -> dict[str, Any]:
    """Normalize the completed staged pair without repeating Start grading."""

    payload = dict(verdict) if isinstance(verdict, dict) else {}
    before = payload.get("before")
    if not isinstance(before, dict):
        before = dict(prior_before)
    after = payload.get("after")
    if not isinstance(after, dict):
        terminal = payload.get("result") is True
        after = {
            "stage": "after",
            "label": "correct" if terminal else "incorrect",
            "compatibility_fallback": True,
        }
    passed = before.get("label") == "correct" and after.get("label") == "correct"
    payload.update(
        {
            "result": passed,
            "status": "success" if passed else "failure",
            "before": before,
            "after": after,
            "after_evaluated": after.get("label") in {"correct", "incorrect"},
        }
    )
    return payload


PAIR_START_GATE_SCHEMA = "c1-paired-start-gate-v1"
PAIR_START_GATE_SCHEMA_VERSION = 1


def _pair_start_projection(spec: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(spec[key])
        for key in ("name", "benchmark", "setup", "target", "checkpoint")
        if key in spec
    }


def _pair_start_cache_identity(
    result: Mapping[str, Any],
    *,
    task: StateLoopTask,
    accepted_spec: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    identity = {
        "schema": PAIR_START_GATE_SCHEMA,
        "schema_version": PAIR_START_GATE_SCHEMA_VERSION,
        "task_id": task.id,
        "benchmark": task.benchmark,
        "seed": int(result.get("seed", 0) or 0),
        "prompt_revision": str(task.prompt_revision or ""),
        "generated_spec_hash": result.get("generated_spec_hash"),
        "synthesis_cache_fingerprint": result.get(
            "synthesis_cache_fingerprint"
        ),
        "accepted_start_sha256": _sha256_json(
            _pair_start_projection(accepted_spec)
        ),
        "visual_judge_model": os.environ.get("WARPTEST_VISUAL_JUDGE_MODEL"),
        "visual_judge_base_url": os.environ.get(
            "WARPTEST_VISUAL_JUDGE_BASE_URL"
        ),
        "visual_protocol": "normalized_image_equality_then_registered_judge_v1",
    }
    return identity, canonical_hash(identity, length=64)


def _read_pair_start_gate(
    path: Path, *, identity: Mapping[str, Any]
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != PAIR_START_GATE_SCHEMA
        or payload.get("schema_version") != PAIR_START_GATE_SCHEMA_VERSION
        or payload.get("identity") != identity
        or payload.get("written_before_transition") is not True
        or not isinstance(payload.get("pair_verdict"), dict)
        or not isinstance(payload.get("start_success"), bool)
    ):
        return None
    return payload


def _apply_pair_start_gate(
    result: dict[str, Any],
    *,
    payload: Mapping[str, Any],
    cache_hit: bool,
    live_before: str,
) -> tuple[bool, dict[str, Any]]:
    local_semantic = result.get("before_semantic_ok") is True
    pair_semantic = payload.get("before_semantic_ok") is True
    live_path = Path(live_before) if live_before else None
    screenshot_informative = bool(
        live_before and _informative_screenshot(live_before)
    )
    local_attestation = {
        "schema_version": 1,
        "restore_semantic_matches_pair": local_semantic == pair_semantic,
        "local_before_semantic_ok": local_semantic,
        "local_screenshot": str(live_before),
        "local_screenshot_sha256": (
            _file_digest(live_path) if live_path is not None else None
        ),
        "screenshot_informative": screenshot_informative,
        "registered_capture_source": True,
        "runtime_healthy": True,
        "pair_cache_hit": bool(cache_hit),
    }
    result["local_start_attestation"] = local_attestation
    result["pair_start_gate"] = copy.deepcopy(dict(payload))
    result["pair_start_fingerprint"] = payload.get("fingerprint")
    result["pair_start_gate_cache_hit"] = bool(cache_hit)
    result["pair_start_grader_ms"] = int(payload.get("grader_ms", 0) or 0)
    if isinstance(payload.get("start_grading_runtime_hold"), Mapping):
        result["start_grading_runtime_hold"] = copy.deepcopy(
            dict(payload["start_grading_runtime_hold"])
        )
    pair_cost = payload.get("cost")
    pair_cost = (
        copy.deepcopy(dict(pair_cost))
        if isinstance(pair_cost, Mapping)
        else cost.CostMeter().summary()
    )
    result["pair_start_actual_cost"] = (
        cost.CostMeter().summary() if cache_hit else copy.deepcopy(pair_cost)
    )
    result["pair_start_attributed_cost"] = copy.deepcopy(pair_cost)
    result["start_grading_started"] = True
    result["visual_grading_started"] = (
        payload.get("before_visual_status") == "evaluated"
    )
    if not local_attestation["restore_semantic_matches_pair"] or (
        pair_semantic and not local_attestation["screenshot_informative"]
    ):
        result.update(
            status="incomplete",
            incomplete=True,
            infrastructure_error=True,
            reason="local Start attestation differs from the shared pair Start gate",
            start_gate_status="pair_start_attestation_incomplete",
            start_success=False,
            goal_evaluation_status="not_evaluated_start_infrastructure",
            goal_success=False,
            e2e_success=False,
            result=False,
        )
        return False, copy.deepcopy(dict(payload["pair_verdict"]))
    verdict = copy.deepcopy(dict(payload["pair_verdict"]))
    result.update(
        pair_verdict=verdict,
        before_semantic_ok=pair_semantic,
        before_semantic_status=str(payload.get("before_semantic_status") or "failure"),
        before_visual_ok=payload.get("before_visual_ok") is True,
        before_visual_status=str(payload.get("before_visual_status") or "incomplete"),
        start_success=payload.get("start_success") is True,
        start_gate_status=str(payload.get("start_gate_status") or "visual_incomplete"),
    )
    if result["start_success"] is not True:
        result.update(
            goal_evaluation_status="not_evaluated_start_failed",
            after_semantic_status="not_evaluated",
            after_visual_status="not_evaluated",
            after_semantic_ok=False,
            after_visual_ok=False,
            goal_success=False,
            e2e_success=False,
            result=False,
        )
    return bool(result["start_success"]), verdict


def _freeze_pair_start_semantic_failure(
    result: dict[str, Any],
    *,
    task: StateLoopTask,
    accepted_spec: Mapping[str, Any],
    synthesis_agent: Any,
    live_before: str | None = None,
) -> None:
    """Persist or consume a complete shared semantic Start failure."""

    pair_cache_value = result.get("_pair_start_cache")
    if not isinstance(pair_cache_value, str) or not pair_cache_value:
        return
    observed_fingerprint = getattr(
        synthesis_agent, "current_cache_fingerprint", None
    )
    if isinstance(observed_fingerprint, str) and observed_fingerprint:
        result["synthesis_cache_fingerprint"] = observed_fingerprint
    identity, fingerprint = _pair_start_cache_identity(
        result, task=task, accepted_spec=accepted_spec
    )
    cache_root = Path(pair_cache_value).expanduser().resolve()
    path = cache_root / fingerprint[:2] / f"{fingerprint}.json"
    pair_verdict = {
        "result": False,
        "status": "failure",
        "before": {
            "stage": "before",
            "label": "not_evaluated",
            "evaluation_status": "not_evaluated_semantic_failed",
        },
        "after": {
            "stage": "after",
            "label": "not_evaluated",
            "evaluation_status": "not_evaluated_start_failed",
        },
        "after_evaluated": False,
    }
    payload = {
        "schema": PAIR_START_GATE_SCHEMA,
        "schema_version": PAIR_START_GATE_SCHEMA_VERSION,
        "identity": identity,
        "fingerprint": fingerprint,
        "written_before_transition": True,
        "before_semantic_ok": False,
        "before_semantic_status": result.get("before_semantic_status") or "failure",
        "before_visual_ok": False,
        "before_visual_status": "not_evaluated_semantic_failed",
        "start_success": False,
        "start_gate_status": "semantic_failed",
        "canonical_before": {
            "path": str(live_before) if live_before else None,
            "sha256": (
                _file_digest(Path(live_before)) if live_before else None
            ),
        },
        "golden_before": {"path": None, "sha256": None},
        "pair_verdict": pair_verdict,
        "visual_protocol": "not_evaluated_semantic_failed",
        "judge_model": None,
        "grader_ms": int(result.get("grader_ms", 0) or 0),
        "cost": cost.CostMeter().summary(),
        "start_grading_runtime_hold": None,
    }
    with runtime_lease(path, lock_dir=cache_root / "locks"):
        cached = _read_pair_start_gate(path, identity=identity)
        if cached is None:
            atomic_write_json(path, payload)
            cached = payload
    result["pair_start_artifact_path"] = str(path)
    _apply_pair_start_gate(
        result,
        payload=cached,
        cache_hit=cached is not payload,
        live_before=str(live_before or ""),
    )


def _freeze_pair_start_construction_failure(
    result: dict[str, Any],
    *,
    task: StateLoopTask,
    pair_start_cache: Path,
) -> None:
    """Bind a shared synthesis/acceptance failure without an accepted spec."""

    identity = {
        "schema": PAIR_START_GATE_SCHEMA,
        "schema_version": PAIR_START_GATE_SCHEMA_VERSION,
        "task_id": task.id,
        "benchmark": task.benchmark,
        "seed": int(result.get("seed", 0) or 0),
        "prompt_revision": str(task.prompt_revision or ""),
        "generated_spec_hash": None,
        "synthesis_cache_fingerprint": result.get(
            "synthesis_cache_fingerprint"
        ),
        "accepted_start_sha256": None,
        "visual_judge_model": os.environ.get("WARPTEST_VISUAL_JUDGE_MODEL"),
        "visual_judge_base_url": os.environ.get(
            "WARPTEST_VISUAL_JUDGE_BASE_URL"
        ),
        "visual_protocol": "not_evaluated_start_construction_failed",
    }
    fingerprint = canonical_hash(identity, length=64)
    path = pair_start_cache / fingerprint[:2] / f"{fingerprint}.json"
    pair_verdict = {
        "result": False,
        "status": "failure",
        "before": {
            "stage": "before",
            "label": "not_evaluated",
            "evaluation_status": "not_evaluated_start_construction_failed",
        },
        "after": {
            "stage": "after",
            "label": "not_evaluated",
            "evaluation_status": "not_evaluated_start_construction_failed",
        },
        "after_evaluated": False,
    }
    payload = {
        "schema": PAIR_START_GATE_SCHEMA,
        "schema_version": PAIR_START_GATE_SCHEMA_VERSION,
        "identity": identity,
        "fingerprint": fingerprint,
        "written_before_transition": True,
        "before_semantic_ok": False,
        "before_semantic_status": "not_evaluated_start_construction_failed",
        "before_visual_ok": False,
        "before_visual_status": "not_evaluated_start_construction_failed",
        "start_success": False,
        "start_gate_status": "not_evaluated_start_construction_failed",
        "canonical_before": {"path": None, "sha256": None},
        "golden_before": {"path": None, "sha256": None},
        "pair_verdict": pair_verdict,
        "visual_protocol": "not_evaluated_start_construction_failed",
        "judge_model": None,
        "grader_ms": 0,
        "cost": cost.CostMeter().summary(),
    }
    with runtime_lease(path, lock_dir=pair_start_cache / "locks"):
        cached = _read_pair_start_gate(path, identity=identity)
        if cached is None:
            atomic_write_json(path, payload)
            cached = payload
    result.update(
        pair_start_gate=copy.deepcopy(cached),
        pair_start_fingerprint=fingerprint,
        pair_start_artifact_path=str(path),
        pair_start_gate_cache_hit=cached is not payload,
        pair_start_actual_cost=cost.CostMeter().summary(),
        pair_start_attributed_cost=cost.CostMeter().summary(),
        local_start_attestation={
            "schema_version": 1,
            "status": "not_evaluated_start_construction_failed",
            "pair_cache_hit": cached is not payload,
        },
        start_success=False,
        start_gate_status="not_evaluated_start_construction_failed",
        goal_evaluation_status="not_evaluated_start_construction_failed",
        goal_success=False,
        e2e_success=False,
        result=False,
    )


def _run_state_start_visual_gate(
    result: dict[str, Any],
    *,
    live_before: str,
    goldens: Any,
    task: StateLoopTask,
    runtime: Any = None,
    no_runtime_backend: str = NO_HOLD_BACKEND,
    description: str | None = None,
    before_rubric: dict[str, list[str]] | None = None,
    accepted_spec: Mapping[str, Any] | None = None,
    synthesis_agent: Any = None,
) -> tuple[bool, dict[str, Any]]:
    """Grade Start before any registered State transition is executed.

    The rubric defaults to the registered one for this benchmark rather than the
    generic rubric: every call site omitted it, so Luanti was graded against a
    rubric that does not tolerate a rendered voxel world while the GUI arm used
    the Luanti rubric for the same Start.

    ``runtime`` is frozen for the duration of the judge request, matching the GUI
    arm. Omitting it records a no-op hold, which is what the report-side gate
    then sees.
    """

    observed_fingerprint = getattr(
        synthesis_agent, "current_cache_fingerprint", None
    )
    if isinstance(observed_fingerprint, str) and observed_fingerprint:
        result["synthesis_cache_fingerprint"] = observed_fingerprint
    if (
        accepted_spec is not None
        and result.get("transition_action_source")
        in PLANNED_TRANSITION_ACTION_SOURCES
    ):
        accepted_actions = accepted_spec.get("actions")
        accepted_assertions = accepted_spec.get("assertions")
        result["original_generated_actions_sha256"] = _sha256_json(
            accepted_actions if isinstance(accepted_actions, list) else []
        )
        result["original_generated_assertions_sha256"] = _sha256_json(
            accepted_assertions if isinstance(accepted_assertions, list) else []
        )
    pair_cache_value = result.get("_pair_start_cache")
    pair_cache_root = (
        Path(pair_cache_value).expanduser().resolve()
        if isinstance(pair_cache_value, str) and pair_cache_value
        else None
    )
    pair_identity: dict[str, Any] | None = None
    pair_fingerprint: str | None = None
    pair_path: Path | None = None
    if pair_cache_root is not None and accepted_spec is not None:
        pair_identity, pair_fingerprint = _pair_start_cache_identity(
            result, task=task, accepted_spec=accepted_spec
        )
        pair_path = (
            pair_cache_root
            / pair_fingerprint[:2]
            / f"{pair_fingerprint}.json"
        )
        with runtime_lease(pair_path, lock_dir=pair_cache_root / "locks"):
            cached = _read_pair_start_gate(pair_path, identity=pair_identity)
        if cached is not None:
            result["pair_start_artifact_path"] = str(pair_path)
            return _apply_pair_start_gate(
                result,
                payload=cached,
                cache_hit=True,
                live_before=live_before,
            )

    pair_cost_marker = cost.mark_records()
    if before_rubric is None:
        before_rubric = before_rubric_for_benchmark(task.benchmark)
    result["start_grading_started"] = True
    result["visual_grading_started"] = True
    grader_started = time.perf_counter()
    hold_metadata: dict[str, Any] = {}
    result["start_grading_runtime_hold"] = hold_metadata
    with hold_runtime_state_for_start_grading(
        runtime, metadata=hold_metadata, no_runtime_backend=no_runtime_backend
    ):
        verdict = _normalize_state_before_verdict(
            run_visual_pair_audit(
                live_before=live_before,
                live_after=None,
                golden_before=goldens.before,
                golden_after=goldens.after,
                description=description
                or task.reference_steps
                or task.state_description,
                before_description=task.preconditions or task.state_description,
                after_description=goldens.after_description,
                before_rubric=before_rubric,
                after_rubric=goldens.after_rubric,
                evaluate_after=False,
            )
        )
    result["start_grading_runtime_hold"] = dict(hold_metadata)
    result["grader_ms"] = int(result.get("grader_ms", 0) or 0) + int(
        (time.perf_counter() - grader_started) * 1000
    )
    if pair_cost_marker is None:
        pair_cost = cost.CostMeter().summary()
    else:
        pair_meter, pair_start = pair_cost_marker
        pair_cost = cost.CostMeter(
            records=list(pair_meter.records[pair_start:])
        ).summary()
    before = verdict["before"]
    label = before.get("label")
    before_ok = label == "correct"
    determinate = label in {"correct", "incorrect"}
    # Replay determinism (C4) grades the visual channel by strict normalized
    # image identity, which is deliberately stricter than the rubric this gate
    # was designed around: the method's restored frame and the hidden oracle's
    # frame legitimately differ on benign detail. Letting that gate Phase B
    # would suppress the transition and silently turn a rendering difference
    # into a programmatic failure, so in replay mode the gate is programmatic
    # and the visual verdict is retained as a diagnostic only.
    programmatic_gate = replay_programmatic_start_gate()
    start_ok = bool(
        result.get("before_semantic_ok") is True and (programmatic_gate or before_ok)
    )
    result.update(
        pair_verdict=verdict,
        before_visual_ok=before_ok,
        before_visual_status="evaluated" if determinate else "incomplete",
        start_success=start_ok,
        start_gate_status=(
            "passed"
            if start_ok
            else "semantic_failed"
            if programmatic_gate
            else "visual_failed"
            if determinate
            else "visual_incomplete"
        ),
    )
    if programmatic_gate:
        result["start_gate_policy"] = "replay_programmatic_only_v1"
    if not start_ok:
        result.update(
            goal_evaluation_status="not_evaluated_start_failed",
            after_semantic_status="not_evaluated",
            after_visual_status="not_evaluated",
            after_semantic_ok=False,
            after_visual_ok=False,
            goal_success=False,
            e2e_success=False,
            result=False,
        )
    if pair_path is not None and pair_identity is not None and pair_fingerprint:
        before_path = Path(live_before)
        golden_path = Path(goldens.before)
        pair_payload = {
            "schema": PAIR_START_GATE_SCHEMA,
            "schema_version": PAIR_START_GATE_SCHEMA_VERSION,
            "identity": pair_identity,
            "fingerprint": pair_fingerprint,
            "written_before_transition": True,
            "before_semantic_ok": result.get("before_semantic_ok") is True,
            "before_semantic_status": result.get("before_semantic_status"),
            "before_visual_ok": before_ok,
            "before_visual_status": result.get("before_visual_status"),
            "start_success": start_ok,
            "start_gate_status": result.get("start_gate_status"),
            "canonical_before": {
                "path": str(before_path),
                "sha256": _file_digest(before_path),
            },
            "golden_before": {
                "path": str(golden_path),
                "sha256": _file_digest(golden_path),
            },
            "pair_verdict": copy.deepcopy(verdict),
            "visual_protocol": (
                (verdict.get("before") or {}).get("verdict_source")
                if isinstance(verdict.get("before"), dict)
                else None
            ),
            "judge_model": (
                (verdict.get("before") or {}).get("judge_model")
                if isinstance(verdict.get("before"), dict)
                else None
            ),
            "grader_ms": int(result.get("grader_ms", 0) or 0),
            "cost": pair_cost,
            "start_grading_runtime_hold": copy.deepcopy(
                result.get("start_grading_runtime_hold")
            ),
        }
        if result.get("before_visual_status") == "evaluated":
            with runtime_lease(pair_path, lock_dir=pair_cache_root / "locks"):
                cached = _read_pair_start_gate(
                    pair_path, identity=pair_identity
                )
                if cached is None:
                    atomic_write_json(pair_path, pair_payload)
                    cached = pair_payload
            result["pair_start_artifact_path"] = str(pair_path)
            return _apply_pair_start_gate(
                result,
                payload=cached,
                cache_hit=cached is not pair_payload,
                live_before=live_before,
            )
    return start_ok, verdict


def _run_state_goal_visual_grade(
    result: dict[str, Any],
    *,
    before_verdict: dict[str, Any],
    live_before: str,
    live_after: str,
    goldens: Any,
    task: StateLoopTask,
    description: str | None = None,
    before_rubric: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Grade Goal using the frozen Start verdict and finalize distinct metrics."""

    if before_rubric is None:
        before_rubric = before_rubric_for_benchmark(task.benchmark)
    prior_before = before_verdict.get("before")
    prior_before = dict(prior_before) if isinstance(prior_before, dict) else {}
    grader_started = time.perf_counter()
    verdict = _normalize_state_goal_verdict(
        run_visual_pair_audit(
            live_before=live_before,
            live_after=live_after,
            golden_before=goldens.before,
            golden_after=goldens.after,
            description=description
            or task.reference_steps
            or task.state_description,
            before_description=task.preconditions or task.state_description,
            after_description=goldens.after_description,
            before_rubric=before_rubric,
            after_rubric=goldens.after_rubric,
            prior_before=prior_before,
        ),
        prior_before=prior_before,
    )
    result["grader_ms"] = int(result.get("grader_ms", 0) or 0) + int(
        (time.perf_counter() - grader_started) * 1000
    )
    after = verdict["after"]
    label = after.get("label")
    determinate = label in {"correct", "incorrect"}
    after_visual_ok = label == "correct"
    goal_ok = bool(result.get("after_semantic_ok") is True and after_visual_ok)
    e2e_ok = bool(result.get("start_success") is True and goal_ok)
    result.update(
        pair_verdict=verdict,
        after_visual_ok=after_visual_ok,
        after_visual_status="evaluated" if determinate else "incomplete",
        goal_evaluation_status="evaluated" if determinate else "incomplete",
        goal_success=goal_ok,
        e2e_success=e2e_ok,
        result=e2e_ok,
    )
    return verdict


def _phase_detail(phase: Any) -> Any:
    """Serialize a phase result for the record without requiring a full one."""

    to_dict = getattr(phase, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return {
        "status": getattr(phase, "status", None),
        "detail": getattr(phase, "detail", None),
    }


def _state_oracle_start_grade(
    profile: Any,
    runtime: Any,
    oracle_spec: dict[str, Any] | None,
) -> tuple[bool, str, dict[str, Any]] | None:
    """Re-validate the restored Start against the registered oracle target.

    Arm E runs with state hooks enabled, so it uses the profile validator
    directly instead of the production-surface grader the hook-free GUI arms
    need.  Returning ``None`` means no oracle-side evidence was obtainable and
    :func:`merge_start_semantic` will fail closed.
    """

    if not isinstance(oracle_spec, dict) or profile is None or runtime is None:
        return None
    return semantic_start_grade(
        profile, runtime, oracle_spec, prefer_public_surface=False
    )


def _oracle_probe_grade(
    probe: Any,
    *,
    ok: bool,
    status: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Wrap a backend-native oracle probe report as a semantic grade."""

    detail = probe if isinstance(probe, dict) else {"report": probe}
    if ok:
        return True, "success", detail
    return False, str(status or "validate_failed"), detail


def _mark_state_start_semantic(
    result: dict[str, Any],
    *,
    ok: bool,
    status: str,
    detail: Any | None = None,
) -> None:
    result.update(
        start_grading_started=True,
        before_semantic_ok=bool(ok),
        before_semantic_status=str(status or ("success" if ok else "failure")),
    )
    if detail is not None:
        result["before_semantic_detail"] = detail
    if not ok:
        result.update(
            start_gate_status="semantic_failed",
            goal_evaluation_status="not_evaluated_start_failed",
            start_success=False,
            goal_success=False,
            e2e_success=False,
            result=False,
        )


def _mark_state_goal_semantic(
    result: dict[str, Any],
    *,
    ok: bool,
    status: str,
    detail: Any | None = None,
) -> None:
    result.update(
        after_semantic_ok=bool(ok),
        after_semantic_status=str(status or ("success" if ok else "failure")),
        goal_evaluation_status="evaluated",
    )
    if detail is not None:
        result["after_semantic_detail"] = detail
    if not ok:
        result.update(
            after_visual_ok=False,
            after_visual_status="not_evaluated_semantic_failed",
            goal_success=False,
            e2e_success=False,
            result=False,
        )


def _mark_state_start_visual_incomplete(
    result: dict[str, Any], *, status: str = "incomplete_capture"
) -> None:
    """Fail closed when Start exists semantically but cannot be judged visually."""

    result.update(
        before_visual_ok=False,
        before_visual_status=status,
        start_gate_status="visual_incomplete",
        start_success=False,
        goal_evaluation_status="not_evaluated_start_failed",
        after_semantic_status="not_evaluated",
        after_visual_status="not_evaluated",
        after_semantic_ok=False,
        after_visual_ok=False,
        goal_success=False,
        e2e_success=False,
        result=False,
    )


def _mark_state_goal_visual_incomplete(
    result: dict[str, Any], *, status: str = "incomplete_capture"
) -> None:
    """Fail closed when Goal exists semantically but cannot be judged visually."""

    result.update(
        after_visual_ok=False,
        after_visual_status=status,
        goal_evaluation_status="incomplete",
        goal_success=False,
        e2e_success=False,
        result=False,
    )


def _ensure_state_stage_metrics(result: dict[str, Any]) -> None:
    if "state_stage_metrics_schema_version" not in result:
        initialize_state_stage_metrics(result)


def _checkpoint_provider_failure(checkpoint: Any) -> str | None:
    """Return a safe provider-failure reason hidden behind a scaffold fallback."""

    payload = getattr(checkpoint, "payload", None)
    validation = getattr(checkpoint, "runtime_validation", None)
    candidates = []
    if isinstance(payload, dict):
        candidates.append(payload.get("model_fallback_reason"))
    if isinstance(validation, dict):
        candidates.append(validation.get("model_fallback_reason"))
    for value in candidates:
        reason = str(value or "")
        lowered = reason.lower()
        category = run_context.error_category(RuntimeError(reason))
        if (
            "api key" in lowered
            or "authentication" in lowered
            or "unauthorized" in lowered
            or "forbidden" in lowered
            or category in {"infra", "rate_limit"}
        ):
            return run_context.safe_error_text(reason, limit=2000)
    return None


def _run_single_task_body(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    *,
    out_dir: Path,
    max_iters: int,
    headless: bool,
    seed: int = 0,
    restore_diagnostics: bool = True,
    source_repo: Path | None = None,
    minimal_planner: Any | None = None,
    transition_action_source: str = "generated_spec",
    pair_start_cache: Path | None = None,
) -> dict[str, Any]:
    issue = task.issue
    task_dir = out_dir / "tasks" / task.id
    result: dict[str, Any] = {
        "task_id": task.id,
        "benchmark": task.benchmark,
        "state_description": task.state_description,
        "preconditions": task.preconditions,
        "reference_steps": task.reference_steps,
        **(
            {"goal_description": task.goal_description}
            if task.goal_description.strip()
            else {}
        ),
        "prompt_revision": str(
            getattr(task, "prompt_revision", "") or ""
        ),
        "horizon_class": task.horizon_class,
        "oracle_spec": task.oracle_spec,
        "seed": seed,
        "result": False,
        "iterations": [],
        # True iff the candidate pair reached the external visual judge. This is
        # consumed by the formal C1 ledger gate; legal synthesis/restore/smoke
        # failures before that boundary must not be charged a nonexistent judge.
        "visual_grading_started": False,
        "restore_diagnostics_enabled": bool(restore_diagnostics),
    }
    if pair_start_cache is not None:
        result["_pair_start_cache"] = str(pair_start_cache)
    initialize_state_stage_metrics(result)
    _initialize_minimal_smoke_result(
        result, transition_action_source=transition_action_source
    )
    try:
        repo = resolve_task_repo(base, issue)
        oracle_spec_path = (base / task.oracle_spec).resolve()
        oracle_spec = _apply_experiment_seed(
            load_spec(oracle_spec_path), seed, benchmark=task.benchmark
        )
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"task setup failed: {error}"
        return result
    result["validity_class"] = str(
        (oracle_spec.get("checkpoint") or {}).get("expected_validity") or "unclassified"
    )
    reference_replay = audit_scripted_reference_replay(
        base,
        task,
        repo,
        oracle_spec,
        oracle_spec_path,
        seed=seed,
        headless=headless,
    )
    result["reference_replay"] = reference_replay
    result["reference_reachable"] = reference_replay.get("reference_reachable")
    if not isinstance(result["reference_reachable"], bool):
        result["status"] = "failed"
        result["incomplete"] = True
        result["reason"] = str(
            reference_replay.get("reason") or "scripted reference replay audit failed"
        )
        return result

    # Engine-native pair evaluation path: Blender renders deterministic before/after
    # PNGs directly through bpy instead of using browser warm-session state.
    if task.benchmark in {"Blender", "blender"}:
        return _run_blender_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            headless=headless,
            source_repo=source_repo,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Public native-settings pair path: OpenRA remains a normal foreground game
    # while its isolated settings.yaml provides the deep semantic oracle.
    if task.benchmark.lower() == "openra" and (oracle_spec.get("target") or {}).get("kind") == "openra_settings":
        return _run_openra_settings_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            seed=seed,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Native gameplay state path: keep one real OpenRA match alive, capture its
    # registered start, then execute the model-bound registered action sequence
    # through the bounded gameplay probe before capturing and grading the goal.
    if task.benchmark.lower() == "openra" and (oracle_spec.get("target") or {}).get("kind") == "openra_map":
        return _run_openra_gameplay_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            seed=seed,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Document-native pair evaluation path: LibreOffice uses isolated UNO/ODF
    # checkpoints plus desktop before/after screenshots instead of browser state.
    if task.benchmark in {"LibreOffice", "libreoffice"}:
        return _run_libreoffice_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            headless=headless,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Unity-native checkpoint pair path. The public task hook is run once with a
    # target-only spec for the start capture and once with the registered actions
    # for the goal capture; no browser StateSession is involved.
    if str(task.benchmark).lower() in {"jynew", "pal3", "pal3.unity"}:
        return _run_unity_checkpoint_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            headless=headless,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Web-native C1 State path. JupyterLab uses its public Contents/kernel
    # primitives and is not compatible with the generic browser StateSession.
    if str(task.benchmark).strip().lower() == "jupyterlab":
        return _run_c1_web_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            headless=headless,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Engine warm-restore path (luanti): a persistent rendered client with skip-spawn
    # and skip-synthesis, distinct from the browser StateSession.
    if task.benchmark in {"luanti", "minetest"}:
        return _run_luanti_task(
            base,
            task,
            agent,
            repo,
            oracle_spec,
            oracle_spec_path,
            result,
            task_dir=task_dir,
            seed=seed,
            restore_diagnostics=restore_diagnostics,
            minimal_planner=minimal_planner,
            transition_action_source=transition_action_source,
        )

    # Synthesize the iteration-1 config up front so the cold baseline (a transient
    # session) can be measured BEFORE the warm session opens. Two sync Playwright
    # instances cannot coexist, so cold measurement must not overlap the warm one.
    seed_dir = task_dir / "v1"
    seed_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        seed_checkpoint = agent.synthesize(issue, base, artifact_dir=seed_dir / "synthesis-artifacts")
    except Exception as error:
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
        result["status"] = "failed"
        result["reason"] = f"config synthesis failed: {error}"
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
    result["checkpoint_synthesis_ms"] = synthesis_ms
    # Use the method's OWN spec, not the canonicalized public-task scaffold -- a
    # model that produced no usable spec (model_generated_spec is None, empty, or
    # missing required fields) fails iteration 1 the same way ``_run_iteration``
    # fails any other bad synthesis output, rather than silently measuring
    # scaffold cold-restore timing.
    seed_spec, _ = _accepted_state_spec(
        seed_checkpoint.model_generated_spec, task=task, seed=seed
    )
    cold_restore_ms: int | None = None
    if seed_spec is not None:
        seed_config_path = seed_dir / "spec.yaml"
        seed_config_path.write_text(
            yaml.safe_dump(seed_spec, sort_keys=False, allow_unicode=False), encoding="utf-8"
        )
        if restore_diagnostics:
            cold_restore_ms = measure_cold_restore(
                repo,
                seed_spec,
                seed_config_path,
                report_dir=task_dir / "cold-baseline",
                headless=headless,
                experiment_seed=seed,
            )
            result["cold_restore_ms"] = cold_restore_ms

    # Resolve a shared golden before starting the candidate warm session.  The
    # state arm still needs that session for its own checkpoint, but a cache hit
    # now avoids all hidden-oracle restores and exposes the same pre-runtime API
    # used by GUI arms that otherwise start a dedicated golden runtime.
    goldens = lookup_cached_oracle_goldens(repo, oracle_spec)

    try:
        session = StateSession(
            repo,
            task_dir,
            headless=headless,
            experiment_seed=seed,
        ).start()
    except StateManagementUnsupported as error:
        result["status"] = "unsupported"
        result["reason"] = str(error)
        _set_state_method_timing(result)
        return result
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"session bootstrap failed: {error}"
        _set_state_method_timing(result)
        return result

    try:
        result["session_bootstrap_ms"] = session.bootstrap_ms
        golden_started = time.perf_counter()
        try:
            if goldens is None:
                goldens = ensure_oracle_goldens(session, oracle_spec, oracle_spec_path)
            result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
        except Exception as error:
            result["status"] = "failed"
            result["reason"] = f"oracle golden generation failed: {error}"
            result["grader_ms"] = int((time.perf_counter() - golden_started) * 1000)
            _set_state_method_timing(result)
            return result
        result["grader_ms"] = int((time.perf_counter() - golden_started) * 1000)

        store = StateStore(session)
        description = task.reference_steps or task.state_description
        feedback: dict[str, Any] | None = None
        for iteration in range(1, max(1, max_iters) + 1):
            iteration_result = _run_iteration(
                base,
                task,
                agent,
                session=session,
                store=store,
                goldens=goldens,
                description=description,
                task_dir=task_dir,
                iteration=iteration,
                feedback=feedback,
                oracle_spec=oracle_spec,
                # Iteration 1 reuses the pre-synthesized seed config + cold baseline.
                seed_checkpoint=seed_checkpoint if iteration == 1 else None,
                cold_restore_ms=cold_restore_ms if iteration == 1 else None,
                experiment_seed=seed,
                probe_fast_vs_full=restore_diagnostics,
                minimal_planner=minimal_planner,
                transition_action_source=transition_action_source,
                pair_start_cache=pair_start_cache,
            )
            result["iterations"].append(iteration_result)
            if iteration_result.get("result"):
                result["result"] = True
                break
            feedback = iteration_result.get("pair_verdict")
        result["status"] = "success" if result["result"] else "failure"
        # Surface the last iteration's headline signals at the task level.
        if result["iterations"]:
            last = result["iterations"][-1]
            result["checkpoint_synthesis_ms"] = int(
                result.get("checkpoint_synthesis_ms", 0) or 0
            ) + sum(
                int(item.get("checkpoint_synthesis_ms", 0) or 0)
                for item in result["iterations"]
            )
            result["restore_timing"] = last.get("restore_timing")
            result["after_timing"] = last.get("after_timing")
            result["screenshot_before"] = last.get("screenshot_before")
            result["screenshot_after"] = last.get("screenshot_after")
            result["initial_state_fingerprint"] = last.get("initial_state_fingerprint")
            result["pair_verdict"] = last.get("pair_verdict")
            for field in (
                "state_stage_metrics_schema_version",
                "start_grading_started",
                "start_gate_status",
                "goal_evaluation_status",
                "before_semantic_status",
                "before_visual_status",
                "after_semantic_status",
                "after_visual_status",
                "before_semantic_ok",
                "before_visual_ok",
                "after_semantic_ok",
                "after_visual_ok",
                "start_success",
                "goal_success",
                "e2e_success",
                "start_grading_runtime_hold",
                "generated_spec_hash",
                "transition_action_source",
                "minimal_smoke_agent",
                "source_transition_agent",
                "original_generated_actions_ignored",
                "original_generated_actions_sha256",
                "original_generated_assertions_sha256",
                "smoke_action_verdict",
                "smoke_action_failure_stage",
                "smoke_action_generation_ms",
                "smoke_action_execution_ms",
                "source_transition_spec_path",
                "pair_start_fingerprint",
                "pair_start_gate",
                "pair_start_artifact_path",
                "pair_start_gate_cache_hit",
                "pair_start_grader_ms",
                "pair_start_actual_cost",
                "pair_start_attributed_cost",
                "local_start_attestation",
            ):
                if field in last:
                    result[field] = last[field]
            result["visual_grading_started"] = any(
                item.get("visual_grading_started") is True
                for item in result["iterations"]
                if isinstance(item, dict)
            )
            result["grader_ms"] = int(result.get("grader_ms", 0) or 0) + int(
                last.get("grader_ms", 0) or 0
            )
        _set_state_method_timing(result)
        return result
    finally:
        session.close()


def _run_iteration(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    *,
    session: StateSession,
    store: StateStore,
    goldens: OracleGoldens,
    description: str,
    task_dir: Path,
    iteration: int,
    feedback: dict[str, Any] | None,
    oracle_spec: dict[str, Any] | None = None,
    seed_checkpoint: Any | None = None,
    cold_restore_ms: int | None = None,
    experiment_seed: int | None = None,
    probe_fast_vs_full: bool = True,
    minimal_planner: Any | None = None,
    transition_action_source: str = "generated_spec",
    pair_start_cache: Path | None = None,
) -> dict[str, Any]:
    issue = task.issue
    version_dir = task_dir / f"v{iteration}"
    version_dir.mkdir(parents=True, exist_ok=True)
    iteration_result: dict[str, Any] = {
        "iteration": iteration,
        "result": False,
        "visual_grading_started": False,
    }
    if pair_start_cache is not None:
        iteration_result["_pair_start_cache"] = str(pair_start_cache)
    initialize_state_stage_metrics(iteration_result)
    _initialize_minimal_smoke_result(
        iteration_result, transition_action_source=transition_action_source
    )
    if seed_checkpoint is not None:
        checkpoint = seed_checkpoint
    else:
        synthesis_started = time.perf_counter()
        try:
            checkpoint = agent.synthesize(
                issue, base, artifact_dir=version_dir / "synthesis-artifacts", feedback=feedback
            )
        except SynthesisError as error:
            iteration_result["checkpoint_synthesis_ms"] = int(
                (time.perf_counter() - synthesis_started) * 1000
            )
            iteration_result["status"] = "synthesis_failure"
            iteration_result["reason"] = str(error)
            return iteration_result
        except Exception as error:
            iteration_result["checkpoint_synthesis_ms"] = int(
                (time.perf_counter() - synthesis_started) * 1000
            )
            iteration_result["status"] = "synthesis_failure"
            iteration_result["reason"] = str(error)
            return iteration_result
        iteration_result["checkpoint_synthesis_ms"] = int(
            (time.perf_counter() - synthesis_started) * 1000
        )

    # Inject the method's OWN spec, not the canonicalized public-task scaffold --
    # otherwise the method arm would silently measure template coverage instead of
    # its own checkpoint synthesis. A ``model_generated_spec`` that is None, empty,
    # or missing required fields (the model produced no usable structured output)
    # is a genuine failed iteration; the validate-and-retry loop still gets another
    # attempt on the next iteration.
    generated_spec, reject_reason = _accepted_state_spec(
        checkpoint.model_generated_spec, task=task, seed=experiment_seed
    )
    if generated_spec is None:
        iteration_result["status"] = "synthesis_failure"
        iteration_result["used_public_scaffold"] = checkpoint.used_public_scaffold
        provider_failure = _checkpoint_provider_failure(checkpoint)
        if provider_failure is not None:
            iteration_result["incomplete"] = True
            iteration_result["reason"] = (
                "checkpoint synthesis provider failure: " + provider_failure
            )
        else:
            iteration_result["reason"] = reject_reason
        return iteration_result

    config_path = version_dir / "spec.yaml"
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False), encoding="utf-8"
    )
    iteration_result["config_path"] = str(config_path)
    iteration_result["used_public_scaffold"] = checkpoint.used_public_scaffold
    iteration_result["injected_spec_source"] = "model_generated_spec"
    iteration_result["state_fingerprint"] = state_fingerprint(session.git_commit, generated_spec)
    iteration_result["generated_spec_hash"] = generated_spec_hash(generated_spec)

    restore_before_kwargs: dict[str, Any] = {
        "screenshot_name": f"before-v{iteration}.png",
        "cold_restore_ms": cold_restore_ms,
    }
    # Preserve compatibility with external StateStore-like adapters whose
    # historical signature already means "probe enabled" when the field is absent.
    if not probe_fast_vs_full:
        restore_before_kwargs["probe_fast_vs_full"] = False
    before_outcome, timing = store.restore_before(
        generated_spec,
        config_path,
        **restore_before_kwargs,
    )
    iteration_result["restore_timing"] = timing.to_dict()
    iteration_result["screenshot_before"] = before_outcome.screenshot
    iteration_result["initial_state_fingerprint"] = _image_fingerprint(before_outcome.screenshot)
    iteration_result["restore_status"] = before_outcome.status
    iteration_result["restore_phase_statuses"] = before_outcome.phase_statuses
    iteration_result["restore_failure_phase"] = before_outcome.failure_phase
    iteration_result["localstorage_keys"] = before_outcome.localstorage_keys
    if before_outcome.status != "success":
        _mark_state_start_semantic(
            iteration_result,
            ok=False,
            status=before_outcome.status,
            detail=before_outcome.report,
        )
        iteration_result["status"] = "restore_failure"
        iteration_result["reason"] = before_outcome.detail or "before-state restore failed"
        return iteration_result

    semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
        restore_ok=True,
        restore_status="success",
        restore_detail=before_outcome.report,
        oracle_grade=_state_oracle_start_grade(
            getattr(session, "_profile", None), session.runtime, oracle_spec
        ),
    )
    _mark_state_start_semantic(
        iteration_result,
        ok=semantic_ok,
        status=semantic_status,
        detail=semantic_detail,
    )
    if not semantic_ok:
        _freeze_pair_start_semantic_failure(
            iteration_result,
            task=task,
            accepted_spec=generated_spec,
            synthesis_agent=agent,
            live_before=before_outcome.screenshot,
        )
        iteration_result["status"] = "failure"
        iteration_result["reason"] = (
            "restored Start does not satisfy the registered oracle target "
            f"({semantic_status})"
        )
        return iteration_result
    if not before_outcome.screenshot or not _informative_screenshot(
        before_outcome.screenshot
    ):
        _mark_state_start_visual_incomplete(iteration_result)
        iteration_result["status"] = "restore_failure"
        iteration_result["reason"] = (
            "before-state screenshot is missing or non-informative"
        )
        return iteration_result
    start_ok, before_verdict = _run_state_start_visual_gate(
        iteration_result,
        live_before=before_outcome.screenshot,
        goldens=goldens,
        task=task,
        runtime=session.runtime,
        description=description,
        accepted_spec=generated_spec,
        synthesis_agent=agent,
    )
    if not start_ok:
        iteration_result["status"] = "failure"
        iteration_result["reason"] = "restored Start failed visual grading"
        return iteration_result

    transition_spec = _plan_minimal_smoke_transition(
        iteration_result,
        planner=minimal_planner,
        base=base,
        task=task,
        generated_spec=generated_spec,
        task_dir=version_dir,
        seed=int(experiment_seed or 0),
    )
    if transition_spec is None:
        return iteration_result
    _mark_transition_action_execution_started(iteration_result)
    after_outcome, after_timing = store.restore_after(
        transition_spec,
        config_path,
        actions=transition_spec.get("actions", []),
        # Model transition, registered-oracle assertions. Passed as a keyword so
        # the spec keeps the snapshot identity restore_before cached it under.
        assertions=copy.deepcopy(semantic_assertions(oracle_spec) or []),
        screenshot_name=f"after-v{iteration}.png",
    )
    iteration_result["after_timing"] = after_timing.to_dict()
    iteration_result["screenshot_after"] = after_outcome.screenshot
    iteration_result["after_status"] = after_outcome.status
    iteration_result["after_phase_statuses"] = after_outcome.phase_statuses
    iteration_result["after_failure_phase"] = after_outcome.failure_phase
    iteration_result["after_source"] = after_outcome.source
    if after_outcome.status != "success":
        _mark_state_goal_semantic(
            iteration_result,
            ok=False,
            status=after_outcome.status,
            detail=after_outcome.report,
        )
        iteration_result["status"] = "smoke_failure"
        iteration_result["reason"] = after_outcome.detail or "after-state restore/smoke failed"
        return iteration_result

    _mark_state_goal_semantic(
        iteration_result,
        ok=True,
        status="success",
        detail=after_outcome.report,
    )
    if not after_outcome.screenshot or not _informative_screenshot(
        after_outcome.screenshot
    ):
        _mark_state_goal_visual_incomplete(iteration_result)
        iteration_result["status"] = "smoke_failure"
        iteration_result["reason"] = (
            "after-state screenshot is missing or non-informative"
        )
        return iteration_result
    _run_state_goal_visual_grade(
        iteration_result,
        before_verdict=before_verdict,
        live_before=before_outcome.screenshot,
        live_after=after_outcome.screenshot,
        goldens=goldens,
        task=task,
        description=description,
    )
    iteration_result["status"] = "success" if iteration_result["result"] else "failure"
    return iteration_result


def _pass_at_k(n: int, c: int, k: int) -> float:
    """HumanEval unbiased pass@k estimator (Chen et al. 2021), numerically stable.

    Estimates the probability that at least one of k samples is correct given n
    samples with c correct: ``1 - C(n-c, k) / C(n, k)``, computed as
    ``1 - prod_{i=n-c+1..n}(1 - k/i)`` to avoid large binomials. pass@1 = c/n.
    """

    if c < 0 or c > n:
        raise ValueError(f"invalid pass count: c={c}, n={n}")
    if k <= 0 or n <= 0:
        return 0.0
    k = min(k, n)  # guard only; run-level callers (see _summarize) ensure n >= k
    if n - c < k:
        return 1.0
    prod = 1.0
    for i in range(n - c + 1, n + 1):
        prod *= 1.0 - k / i
    return 1.0 - prod


def _aggregate_seed_results(task: StateLoopTask, seed_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold per-seed runs into one task record with sample counts and summed cost.

    Records ``seed_count`` (n) and ``pass_count`` (c) so the run-level HumanEval
    pass@1/pass@k can be recomputed in :func:`_summarize`. The headline fields mirror
    the best seed (a pass if any seed passed); per-seed detail is kept under `seeds`.
    """

    passes = [bool(r.get("result")) for r in seed_results]
    n = len(seed_results)
    c = sum(1 for p in passes if p)
    best = next((r for r in seed_results if r.get("result")), seed_results[0])
    aggregate = dict(best)
    aggregate["seeds"] = seed_results
    aggregate["seed_count"] = n
    aggregate["pass_count"] = c
    aggregate["pass_at_1"] = _pass_at_k(n, c, 1)  # = c/n
    # Per-task pass@budget where budget == the number of collected seeds for THIS task.
    # Named pass_at_n (not pass_at_k) so it is not confused with the run-level pass@seeds,
    # which _summarize computes only over tasks with seed_count >= seeds.
    aggregate["pass_at_n"] = _pass_at_k(n, c, n)
    aggregate["result"] = any(passes)
    aggregate["visual_grading_started"] = any(
        result.get("visual_grading_started") is True for result in seed_results
    )
    aggregate["cost"] = _sum_costs([r.get("cost") for r in seed_results])
    return aggregate


def _sum_costs(costs: list[Any]) -> dict[str, Any]:
    total = {"llm_calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "llm_latency_ms": 0}
    for entry in costs:
        if not isinstance(entry, dict):
            continue
        for key in total:
            value = entry.get(key)
            if isinstance(value, (int, float)):
                total[key] += value
    total["est_usd"] = cost.estimate_usd(total["prompt_tokens"], total["completion_tokens"])
    return total


def _state_semantic_fingerprint(observation: dict[str, Any]) -> str:
    canonical = json.dumps(
        observation,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_state_evidence_benchmark(benchmark: Any) -> str:
    normalized = str(benchmark or "").strip().lower()
    if normalized == "openra":
        return "OpenRA"
    if normalized == "jynew":
        return "jynew"
    if normalized in {"pal3", "pal3.unity"}:
        return "pal3"
    return str(benchmark or "")


def _openra_state_seed_snapshot(
    live_evidence: dict[str, Any],
    grade: Any,
    *,
    task_id: str,
    seed: int,
    stage: str,
) -> dict[str, Any]:
    detail = grade.detail if isinstance(getattr(grade, "detail", None), dict) else {}
    observation = {
        "source": "openra_public_settings_yaml",
        "live": live_evidence.get("process_alive_at_observation") is True,
        "status": str(getattr(grade, "status", "") or ""),
        "ok": getattr(grade, "ok", None),
        "checks": copy.deepcopy(detail.get("checks")),
        "mismatches": copy.deepcopy(detail.get("mismatches")),
        "settings": copy.deepcopy(live_evidence.get("semantic_fingerprint")),
    }
    return {
        "schema_version": 1,
        "source": OPENRA_STATE_SNAPSHOT_SOURCE,
        "observed": live_evidence.get("observed") is True,
        "algorithm": OPENRA_STATE_PAIR_ALGORITHM,
        "seed": live_evidence.get("seed"),
        "task_id": str(live_evidence.get("task_id") or ""),
        "benchmark": str(live_evidence.get("benchmark") or ""),
        "stage": stage,
        "process_id": live_evidence.get("process_id"),
        "process_alive_at_observation": live_evidence.get(
            "process_alive_at_observation"
        ),
        "semantic_observation": observation,
        "semantic_fingerprint": _state_semantic_fingerprint(observation),
        "expected_task_id": task_id,
        "expected_seed": int(seed),
    }


def _openra_gameplay_state_seed_snapshot(
    live_evidence: dict[str, Any],
    grade: Any,
    *,
    task_id: str,
    seed: int,
    stage: str,
) -> dict[str, Any]:
    detail = grade.detail if isinstance(getattr(grade, "detail", None), dict) else {}
    observation = {
        "source": "openra_live_gameplay_probe_v1",
        "live": live_evidence.get("process_alive_at_observation") is True,
        "status": str(getattr(grade, "status", "") or ""),
        "ok": getattr(grade, "ok", None),
        "checks": copy.deepcopy(detail.get("checks")),
        "errors": copy.deepcopy(detail.get("errors")),
        "world_tick": detail.get("world_tick"),
        "actions_executed": detail.get("actions_executed"),
        "actions_total": detail.get("actions_total"),
    }
    return {
        "schema_version": 1,
        "source": OPENRA_STATE_SNAPSHOT_SOURCE,
        "observed": live_evidence.get("observed") is True,
        "algorithm": OPENRA_GAMEPLAY_STATE_PAIR_ALGORITHM,
        "seed": live_evidence.get("seed"),
        "task_id": str(live_evidence.get("task_id") or ""),
        "benchmark": str(live_evidence.get("benchmark") or ""),
        "stage": stage,
        "process_id": live_evidence.get("process_id"),
        "process_alive_at_observation": live_evidence.get(
            "process_alive_at_observation"
        ),
        "semantic_observation": observation,
        "semantic_fingerprint": _state_semantic_fingerprint(observation),
        "expected_task_id": task_id,
        "expected_seed": int(seed),
    }


def _unity_state_seed_snapshot(
    runtime_result: dict[str, Any],
    *,
    benchmark: str,
    task_id: str,
    seed: int,
    stage: str,
) -> dict[str, Any]:
    report = runtime_result.get("utility_report")
    report = report if isinstance(report, dict) else {}
    checks = report.get("checks")
    checks = copy.deepcopy(checks) if isinstance(checks, list) else []
    reported_benchmark = str(report.get("evidence_benchmark") or "")
    observation = {
        "source": f"{reported_benchmark.lower()}_checkpoint_utility_report_v1",
        "live": report.get("process_alive_at_observation") is True,
        "status": str(report.get("status") or ""),
        "detail": str(report.get("detail") or ""),
        "checks": checks,
        "screenshot_status": str(report.get("screenshot_status") or ""),
        "screenshot_source": str(report.get("screenshot_source") or ""),
    }
    return {
        "schema_version": 1,
        "source": UNITY_STATE_SNAPSHOT_SOURCE,
        "observed": report.get("process_alive_at_observation") is True,
        "algorithm": UNITY_STATE_PAIR_ALGORITHM,
        "seed": report.get("evidence_seed"),
        "task_id": str(report.get("evidence_task_id") or ""),
        "benchmark": reported_benchmark,
        "stage": str(report.get("evidence_stage") or ""),
        "process_id": report.get("process_id"),
        "process_alive_at_observation": report.get(
            "process_alive_at_observation"
        ),
        "protocol_version": report.get("evidence_version"),
        "semantic_observation": observation,
        "semantic_fingerprint": _state_semantic_fingerprint(observation),
        "expected_task_id": task_id,
        "expected_seed": int(seed),
        "expected_stage": stage,
        "expected_benchmark": _canonical_state_evidence_benchmark(benchmark),
    }


def _record_state_process_pair_evidence(
    result: dict[str, Any],
    *,
    source: str,
    algorithm: str,
    initial: dict[str, Any],
    final: dict[str, Any],
    pair_semantics: str = "separate_registered_before_after_processes",
) -> None:
    evidence = {
        "schema_version": 1,
        "source": source,
        "observed": bool(
            initial.get("observed") is True and final.get("observed") is True
        ),
        "algorithm": algorithm,
        "seed": result.get("seed"),
        "task_id": str(result.get("task_id") or ""),
        "benchmark": str(initial.get("benchmark") or ""),
        "pair_semantics": pair_semantics,
        "initial": initial,
        "final": final,
    }
    result["runtime_started"] = True
    result["seed_evidence"] = evidence
    result["prng"] = copy.deepcopy(final)


def _record_state_process_start_evidence(
    result: dict[str, Any],
    *,
    source: str,
    algorithm: str,
    initial: dict[str, Any],
    pair_semantics: str = "start_gate_terminal",
) -> None:
    """Bind native provenance when Start legitimately prevents Goal execution."""

    result["runtime_started"] = True
    result["seed_evidence"] = {
        "schema_version": 2,
        "source": source,
        "observed": initial.get("observed") is True,
        "algorithm": algorithm,
        "seed": result.get("seed"),
        "task_id": str(result.get("task_id") or ""),
        "benchmark": str(initial.get("benchmark") or ""),
        "pair_semantics": pair_semantics,
        "terminal_stage": "start",
        "initial": initial,
        "final": None,
    }
    result["prng"] = copy.deepcopy(initial)


def _record_state_runtime_not_started(result: dict[str, Any]) -> None:
    result["runtime_started"] = False
    result["prng"] = None
    result["seed_evidence"] = {
        "schema_version": 1,
        "source": "runtime_not_started",
        "observed": False,
        "algorithm": "not_applicable",
        "seed": result.get("seed"),
        "task_id": str(result.get("task_id") or ""),
        "benchmark": _canonical_state_evidence_benchmark(
            result.get("benchmark")
        ),
    }


def _record_state_runtime_attempted(result: dict[str, Any]) -> None:
    """Record a truthful fail-closed marker until both live snapshots exist."""

    result["runtime_started"] = True
    result["prng"] = None
    result["seed_evidence"] = {
        "schema_version": 1,
        "source": "runtime_started_without_complete_pair",
        "observed": False,
        "algorithm": "not_applicable",
        "seed": result.get("seed"),
        "task_id": str(result.get("task_id") or ""),
        "benchmark": _canonical_state_evidence_benchmark(
            result.get("benchmark")
        ),
    }


def _measure_luanti_cold(
    repo: Path,
    spec: dict[str, Any],
    report_dir: Path,
    *,
    seed: int = 0,
) -> int | None:
    """Cold baseline: a fresh headless engine process per restore (skip-spawn OFF)."""
    session = LuantiWarmSession(repo, report_dir, render=False, experiment_seed=seed)
    try:
        session.start()
    except Exception:
        return None
    try:
        outcome = session.restore(spec, actions=[], assertions=[])
    finally:
        session.close()
    if outcome.status != "success":
        return None
    return (session.bootstrap_ms or 0) + outcome.elapsed_ms


def _native_state_benchmark_errors(
    spec: Any,
    *,
    oracle_spec: dict[str, Any],
    task: StateLoopTask,
) -> list[str]:
    """Reject a candidate written for a different game.

    This used to additionally require the candidate spec to equal the registered
    oracle byte-for-byte on target, actions, assertions and checkpoint contract.
    That was the only thing keeping the goal honest while OpenRA and Unity graded
    themselves with their own assertions, but it also rejected the trial before
    the runtime booted, so a spec that merely differed produced no Start
    datapoint at all -- a black hole the GUI arms do not have. Now that every
    goal is graded by ``semantic_assertions(oracle_spec)`` and every start is
    re-probed against ``oracle_spec["target"]`` on the live session, a wrong
    target is measured as a failed Start rather than pre-rejected, matching how
    the GUI arms treat their own synthesized spec.
    """

    if not isinstance(spec, dict):
        return ["generated_spec must be a mapping"]
    if not isinstance(oracle_spec, dict):
        return ["registered task oracle is not a mapping"]

    errors: list[str] = []
    # Compare with the shared normalizer, not the evidence-label helper: that one
    # deliberately preserves the registered display casing ("OpenRA") and returns
    # unknown benchmarks unchanged, so it made a spec saying "Blender" mismatch a
    # task registered as "blender". The state arm then scored a fair zero on
    # specs the GUI arm accepted and graded, which is exactly the cross-arm
    # asymmetry the shared acceptance exists to remove.
    if canonical_c1_benchmark(spec.get("benchmark")) != canonical_c1_benchmark(
        task.benchmark
    ):
        errors.append("generated_spec benchmark does not match the registered task")
    return errors


def _unity_state_model_spec_errors(
    spec: Any,
    *,
    task: StateLoopTask,
    oracle_spec: dict[str, Any],
    spec_path: Path,
) -> list[str]:
    """Validate an arm-E Unity candidate before any runtime or grader starts."""

    if canonical_c1_benchmark(task.benchmark) not in UNITY_SPEC_CONTRACTS:
        return [f"unsupported Unity benchmark {task.benchmark!r}"]
    errors = list(
        generated_spec_acceptance_errors(
            spec, issue=task.issue, benchmark=task.benchmark
        )
    )
    if not errors:
        errors.extend(validate_spec_schema(spec, spec_path))
    errors.extend(
        _native_state_benchmark_errors(
            spec,
            oracle_spec=oracle_spec,
            task=task,
        )
    )
    return list(dict.fromkeys(errors))


def _run_openra_gameplay_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    seed: int,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    """Run arm E as a bounded transition inside one live OpenRA match."""

    _ensure_state_stage_metrics(result)
    _record_state_runtime_not_started(result)
    result["injected_spec_source"] = "none"
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(
            task.issue,
            base,
            artifact_dir=version_dir / "synthesis-artifacts",
        )
    except Exception as error:
        result.update(
            status="failed",
            reason=f"config synthesis failed: {error}",
            checkpoint_synthesis_ms=int(
                (time.perf_counter() - synthesis_started) * 1000
            ),
        )
        return result
    result["checkpoint_synthesis_ms"] = int(
        (time.perf_counter() - synthesis_started) * 1000
    )
    result["used_public_scaffold"] = bool(
        getattr(checkpoint, "used_public_scaffold", False)
    )
    model_spec, reject_reason = _accepted_state_spec(
        getattr(checkpoint, "model_generated_spec", None), task=task, seed=seed
    )
    if model_spec is None:
        provider_failure = _checkpoint_provider_failure(checkpoint)
        result.update(status="failure", reason=reject_reason)
        if provider_failure is not None:
            result.update(
                status="incomplete",
                incomplete=True,
                reason="checkpoint synthesis provider failure: " + provider_failure,
            )
        return result

    compatibility_errors = validate_spec_schema(
        model_spec,
        oracle_spec_path,
    )
    compatibility_errors.extend(
        _native_state_benchmark_errors(
            model_spec,
            oracle_spec=oracle_spec,
            task=task,
        )
    )
    target = model_spec.get("target") if isinstance(model_spec, dict) else None
    if not isinstance(target, dict) or target.get("kind") != "openra_map":
        compatibility_errors.append(
            "generated_spec target kind is not supported by OpenRA gameplay"
        )
    compatibility_errors = list(dict.fromkeys(compatibility_errors))
    if compatibility_errors:
        result.update(
            status="failure",
            generated_spec_contract_errors=compatibility_errors,
            reason=(
                "OpenRA model-generated spec is incompatible with the registered "
                "gameplay task"
            ),
        )
        return result

    generated_spec = copy.deepcopy(model_spec)
    actions = generated_spec.get("actions")
    actions = actions if isinstance(actions, list) else []
    # Goal is graded by the registered oracle, never by the candidate's own
    # assertions: a model must not be able to weaken its own success test. The
    # transition itself stays the model's, mirroring the GUI arm's free-form
    # Phase B.
    assertions = copy.deepcopy(semantic_assertions(oracle_spec) or [])
    config_path = version_dir / "spec.yaml"
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result.update(
        config_path=str(config_path),
        injected_spec_source="model_generated_spec",
        state_fingerprint=state_fingerprint(
            _repo_git_commit(repo), generated_spec
        ),
        generated_spec_hash=generated_spec_hash(generated_spec),
    )

    try:
        goldens = _registered_desktop_state_goldens(
            repo,
            oracle_spec,
            oracle_spec_path,
            task_dir / "oracle",
            benchmark="OpenRA",
        )
        result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
    except Exception as error:
        result.update(
            status="incomplete",
            incomplete=True,
            reason=f"OpenRA registered golden validation failed: {error}",
        )
        return result

    restore_started = time.perf_counter()
    _record_state_runtime_attempted(result)

    try:
        with runtime_lease(repo):
            with OpenRAGameplaySession(
                repo,
                task_dir / "candidate-runtime",
                spec=generated_spec,
                experiment_seed=seed,
            ) as runtime:
                before_ready_ms = int(
                    (time.perf_counter() - restore_started) * 1000
                )
                before_grade = runtime.grade_target(generated_spec)
                before_shot = runtime.screenshot(task_dir / "candidate-before.png")
                before_seed_snapshot = _openra_gameplay_state_seed_snapshot(
                    runtime.seed_evidence(task.id),
                    before_grade,
                    task_id=task.id,
                    seed=seed,
                    stage="before",
                )
                result.update(
                    screenshot_before=before_shot,
                    checkpoint_restore_ms=before_ready_ms,
                    restore_timing={
                        "cold_restore_ms": before_ready_ms,
                        "warm_restore_ms": None,
                        "speedup": None,
                        "fast_restore_ms": None,
                        "fast_vs_full_speedup": None,
                        "fast_restore_used": False,
                        "fast_restore_fallback": False,
                        "cache_hit": False,
                    },
                )
                oracle_grade = runtime.grade_target(oracle_spec)
                semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
                    restore_ok=bool(before_grade.ok),
                    restore_status=str(before_grade.status),
                    restore_detail=before_grade.detail,
                    oracle_grade=_oracle_probe_grade(
                        oracle_grade.detail,
                        ok=bool(oracle_grade.ok),
                        status=str(oracle_grade.status),
                    ),
                )
                _mark_state_start_semantic(
                    result,
                    ok=semantic_ok,
                    status=semantic_status,
                    detail=semantic_detail,
                )
                if not semantic_ok:
                    _freeze_pair_start_semantic_failure(
                        result,
                        task=task,
                        accepted_spec=generated_spec,
                        synthesis_agent=agent,
                        live_before=before_shot,
                    )
                    _record_state_process_start_evidence(
                        result,
                        source=OPENRA_GAMEPLAY_STATE_PAIR_SOURCE,
                        algorithm=OPENRA_GAMEPLAY_STATE_PAIR_ALGORITHM,
                        initial=before_seed_snapshot,
                    )
                    result.update(
                        status="failure",
                        reason=(
                            "OpenRA gameplay Start semantic validation failed "
                            f"({semantic_status})"
                        ),
                        iterations=[
                            {
                                "iteration": 1,
                                "result": False,
                                "status": "semantic_failure",
                                "screenshot_before": before_shot,
                                "before_semantic_ok": False,
                                "start_success": False,
                                "goal_success": False,
                                "e2e_success": False,
                            }
                        ],
                    )
                    return result
                if not _informative_screenshot(before_shot):
                    _mark_state_start_visual_incomplete(result)
                    _record_state_process_start_evidence(
                        result,
                        source=OPENRA_GAMEPLAY_STATE_PAIR_SOURCE,
                        algorithm=OPENRA_GAMEPLAY_STATE_PAIR_ALGORITHM,
                        initial=before_seed_snapshot,
                    )
                    result.update(
                        status="failure",
                        reason=(
                            "OpenRA gameplay Start screenshot is missing or "
                            "non-informative"
                        ),
                    )
                    return result
                start_ok, before_verdict = _run_state_start_visual_gate(
                    result,
                    live_before=before_shot,
                    goldens=goldens,
                    task=task,
                    runtime=runtime,
                    accepted_spec=generated_spec,
                    synthesis_agent=agent,
                )
                if not start_ok:
                    _record_state_process_start_evidence(
                        result,
                        source=OPENRA_GAMEPLAY_STATE_PAIR_SOURCE,
                        algorithm=OPENRA_GAMEPLAY_STATE_PAIR_ALGORITHM,
                        initial=before_seed_snapshot,
                    )
                    result.update(
                        status="failure",
                        reason="OpenRA gameplay Start visual validation failed",
                        iterations=[
                            {
                                "iteration": 1,
                                "result": False,
                                "status": "start_gate_failure",
                                "screenshot_before": before_shot,
                                "pair_verdict": before_verdict,
                                "start_success": False,
                                "goal_success": False,
                                "e2e_success": False,
                            }
                        ],
                    )
                    return result
                transition_spec = _plan_minimal_smoke_transition(
                    result,
                    planner=minimal_planner,
                    base=base,
                    task=task,
                    generated_spec=generated_spec,
                    task_dir=version_dir,
                    seed=seed,
                )
                if transition_spec is None:
                    return result
                actions = transition_spec.get("actions")
                actions = actions if isinstance(actions, list) else []
                transition_started = time.perf_counter()
                _mark_transition_action_execution_started(result)
                after_grade = runtime.execute_actions_and_grade(
                    actions,
                    assertions,
                )
                transition_ms = int(
                    (time.perf_counter() - transition_started) * 1000
                )
                after_shot = runtime.screenshot(task_dir / "candidate-after.png")
                after_seed_snapshot = _openra_gameplay_state_seed_snapshot(
                    runtime.seed_evidence(task.id),
                    after_grade,
                    task_id=task.id,
                    seed=seed,
                    stage="after",
                )
    except Exception as error:
        result.update(
            status="incomplete",
            incomplete=True,
            reason=f"OpenRA gameplay runtime failed: {error}",
        )
        return result

    _record_state_process_pair_evidence(
        result,
        source=OPENRA_GAMEPLAY_STATE_PAIR_SOURCE,
        algorithm=OPENRA_GAMEPLAY_STATE_PAIR_ALGORITHM,
        initial=before_seed_snapshot,
        final=after_seed_snapshot,
        pair_semantics="same_persistent_registered_gameplay_process",
    )
    restore_ms = int((time.perf_counter() - restore_started) * 1000)
    timing = {
        "cold_restore_ms": before_ready_ms,
        "warm_restore_ms": transition_ms,
        "speedup": (
            before_ready_ms / transition_ms if transition_ms > 0 else None
        ),
        "fast_restore_ms": None,
        "fast_vs_full_speedup": None,
        "fast_restore_used": False,
        "fast_restore_fallback": False,
        "cache_hit": False,
    }
    result.update(
        restore_timing=timing,
        checkpoint_restore_ms=restore_ms,
        screenshot_before=before_shot,
        screenshot_after=after_shot,
        semantic_status=after_grade.status,
        semantic_detail=after_grade.detail,
    )
    _mark_state_goal_semantic(
        result,
        ok=bool(after_grade.ok),
        status=str(after_grade.status),
        detail=after_grade.detail,
    )
    if not after_grade.ok:
        result.update(
            status="failure",
            reason="OpenRA gameplay Goal semantic validation failed",
            iterations=[
                {
                    "iteration": 1,
                    "result": False,
                    "status": "semantic_failure",
                    "restore_timing": timing,
                    "screenshot_before": before_shot,
                    "screenshot_after": after_shot,
                    "pair_verdict": before_verdict,
                    "start_success": True,
                    "goal_success": False,
                    "e2e_success": False,
                }
            ],
        )
        return result
    if not _informative_screenshot(after_shot):
        _mark_state_goal_visual_incomplete(result)
        result.update(
            status="failure",
            reason=(
                "OpenRA gameplay Goal screenshot is missing or non-informative"
            ),
        )
        return result

    verdict = _run_state_goal_visual_grade(
        result,
        before_verdict=before_verdict,
        live_before=before_shot,
        live_after=after_shot,
        goldens=goldens,
        task=task,
    )
    result["status"] = "success" if result["result"] else "failure"
    result["iterations"] = [
        {
            "iteration": 1,
            "result": result["result"],
            "status": result["status"],
            "restore_timing": timing,
            "screenshot_before": before_shot,
            "screenshot_after": after_shot,
            "pair_verdict": verdict,
            "start_success": result["start_success"],
            "goal_success": result["goal_success"],
            "e2e_success": result["e2e_success"],
        }
    ]
    return result


def _run_openra_settings_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    seed: int,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    """Run arm E for an OpenRA public-settings transition.

    Candidate synthesis supplies only the target/settings/action recipe.  The
    normal OpenRA shell is launched once for the before state and once after
    applying the candidate's public setting mutation, so the screenshot and
    semantic grader both observe a state the engine actually loaded.
    """

    _ensure_state_stage_metrics(result)
    _record_state_runtime_not_started(result)
    result["injected_spec_source"] = "none"
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(
            task.issue,
            base,
            artifact_dir=version_dir / "synthesis-artifacts",
        )
    except Exception as error:
        result.update(
            status="failed",
            reason=f"config synthesis failed: {error}",
            checkpoint_synthesis_ms=int(
                (time.perf_counter() - synthesis_started) * 1000
            ),
        )
        return result
    result["checkpoint_synthesis_ms"] = int(
        (time.perf_counter() - synthesis_started) * 1000
    )
    result["used_public_scaffold"] = bool(
        getattr(checkpoint, "used_public_scaffold", False)
    )
    model_spec, reject_reason = _accepted_state_spec(
        getattr(checkpoint, "model_generated_spec", None), task=task, seed=seed
    )
    if model_spec is None:
        provider_failure = _checkpoint_provider_failure(checkpoint)
        result.update(status="failure", reason=reject_reason)
        if provider_failure is not None:
            result.update(
                status="incomplete",
                incomplete=True,
                reason="checkpoint synthesis provider failure: " + provider_failure,
            )
        return result
    compatibility_errors = validate_spec_schema(
        model_spec,
        version_dir / "spec.yaml",
    )
    compatibility_errors.extend(
        _native_state_benchmark_errors(
            model_spec,
            oracle_spec=oracle_spec,
            task=task,
        )
    )
    compatibility_errors = list(dict.fromkeys(compatibility_errors))
    if compatibility_errors:
        result.update(
            status="failure",
            generated_spec_contract_errors=compatibility_errors,
            reason=(
                "OpenRA model-generated spec is incompatible with the registered "
                "public-settings task"
            ),
        )
        return result
    try:
        generated_spec = copy.deepcopy(model_spec)
        start_settings = openra_target_settings(generated_spec)
        if transition_action_source not in PLANNED_TRANSITION_ACTION_SOURCES:
            raw_actions = generated_spec.get("actions")
            raw_actions = raw_actions if isinstance(raw_actions, list) else []
            goal_settings = apply_openra_setting_actions(start_settings, raw_actions)
        # Registered-oracle assertions only; see _run_openra_gameplay_task.
        assertions = copy.deepcopy(semantic_assertions(oracle_spec) or [])
    except Exception as error:
        result.update(
            status="failure",
            reason=f"OpenRA model-generated spec is incompatible: {error}",
        )
        return result

    config_path = version_dir / "spec.yaml"
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result.update(
        config_path=str(config_path),
        injected_spec_source="model_generated_spec",
        state_fingerprint=state_fingerprint(
            _repo_git_commit(repo), generated_spec
        ),
        generated_spec_hash=generated_spec_hash(generated_spec),
    )

    try:
        goldens = _ensure_openra_settings_goldens(
            repo,
            oracle_spec,
            oracle_spec_path,
            task_dir / "oracle",
            seed=seed,
        )
        result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
    except Exception as error:
        result.update(
            status="failed",
            reason=f"oracle golden generation failed: {error}",
        )
        return result

    restore_started = time.perf_counter()
    _record_state_runtime_attempted(result)
    try:
        with contextlib.nullcontext():
            with OpenRASettingsSession(
                repo,
                task_dir / "candidate-before",
                settings=start_settings,
                experiment_seed=seed,
            ) as before_runtime:
                before_grade = before_runtime.grade_settings(start_settings)
                try:
                    oracle_settings_grade = before_runtime.grade_settings(
                        openra_target_settings(oracle_spec)
                    )
                    start_oracle_grade = _oracle_probe_grade(
                        oracle_settings_grade.detail,
                        ok=bool(oracle_settings_grade.ok),
                        status=str(oracle_settings_grade.status),
                    )
                except Exception as error:  # noqa: BLE001 - graded, not raised
                    start_oracle_grade = (
                        False,
                        "validate_error",
                        {"error": str(error)},
                    )
                before_shot = before_runtime.screenshot(
                    task_dir / "candidate-before.png"
                )
                before_seed_snapshot = _openra_state_seed_snapshot(
                    before_runtime.seed_evidence(task.id),
                    before_grade,
                    task_id=task.id,
                    seed=seed,
                    stage="before",
                )
            before_ready_ms = int((time.perf_counter() - restore_started) * 1000)
            result.update(
                screenshot_before=before_shot,
                checkpoint_restore_ms=before_ready_ms,
                restore_timing={
                    "cold_restore_ms": before_ready_ms,
                    "warm_restore_ms": None,
                    "speedup": None,
                    "fast_restore_ms": None,
                    "fast_vs_full_speedup": None,
                    "fast_restore_used": False,
                    "fast_restore_fallback": False,
                    "cache_hit": False,
                },
            )
            semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
                restore_ok=bool(before_grade.ok),
                restore_status=str(before_grade.status),
                restore_detail=before_grade.detail,
                oracle_grade=start_oracle_grade,
            )
            _mark_state_start_semantic(
                result,
                ok=semantic_ok,
                status=semantic_status,
                detail=semantic_detail,
            )
            if not semantic_ok:
                _freeze_pair_start_semantic_failure(
                    result,
                    task=task,
                    accepted_spec=generated_spec,
                    synthesis_agent=agent,
                    live_before=before_shot,
                )
                _record_state_process_start_evidence(
                    result,
                    source=OPENRA_STATE_PAIR_SOURCE,
                    algorithm=OPENRA_STATE_PAIR_ALGORITHM,
                    initial=before_seed_snapshot,
                )
                result.update(
                    status="failure",
                    reason=(
                        "OpenRA public-settings Start semantic validation failed "
                        f"({semantic_status})"
                    ),
                    iterations=[
                        {
                            "iteration": 1,
                            "result": False,
                            "status": "semantic_failure",
                            "screenshot_before": before_shot,
                            "start_success": False,
                            "goal_success": False,
                            "e2e_success": False,
                        }
                    ],
                )
                return result
            if not _informative_screenshot(before_shot):
                _mark_state_start_visual_incomplete(result)
                _record_state_process_start_evidence(
                    result,
                    source=OPENRA_STATE_PAIR_SOURCE,
                    algorithm=OPENRA_STATE_PAIR_ALGORITHM,
                    initial=before_seed_snapshot,
                )
                result.update(
                    status="failure",
                    reason=(
                        "OpenRA public-settings Start screenshot is missing or "
                        "non-informative"
                    ),
                )
                return result
            start_ok, before_verdict = _run_state_start_visual_gate(
                result,
                live_before=before_shot,
                goldens=goldens,
                task=task,
                runtime=before_runtime,
                accepted_spec=generated_spec,
                synthesis_agent=agent,
            )
            if not start_ok:
                _record_state_process_start_evidence(
                    result,
                    source=OPENRA_STATE_PAIR_SOURCE,
                    algorithm=OPENRA_STATE_PAIR_ALGORITHM,
                    initial=before_seed_snapshot,
                )
                result.update(
                    status="failure",
                    reason="OpenRA public-settings Start visual validation failed",
                    iterations=[
                        {
                            "iteration": 1,
                            "result": False,
                            "status": "start_gate_failure",
                            "screenshot_before": before_shot,
                            "pair_verdict": before_verdict,
                            "start_success": False,
                            "goal_success": False,
                            "e2e_success": False,
                        }
                    ],
                )
                return result
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES:
                transition_spec = _plan_minimal_smoke_transition(
                    result,
                    planner=minimal_planner,
                    base=base,
                    task=task,
                    generated_spec=generated_spec,
                    task_dir=version_dir,
                    seed=seed,
                )
                if transition_spec is None:
                    return result
                raw_actions = transition_spec.get("actions")
                raw_actions = raw_actions if isinstance(raw_actions, list) else []
                goal_settings = apply_openra_setting_actions(
                    start_settings, raw_actions
                )
            transition_started = time.perf_counter()
            _mark_transition_action_execution_started(result)
            with OpenRASettingsSession(
                repo,
                task_dir / "candidate-after",
                settings=goal_settings,
                experiment_seed=seed,
            ) as after_runtime:
                after_grade = after_runtime.grade_assertions(assertions)
                after_shot = after_runtime.screenshot(
                    task_dir / "candidate-after.png"
                )
                after_seed_snapshot = _openra_state_seed_snapshot(
                    after_runtime.seed_evidence(task.id),
                    after_grade,
                    task_id=task.id,
                    seed=seed,
                    stage="after",
                )
            transition_ms = int((time.perf_counter() - transition_started) * 1000)
    except Exception as error:
        result.update(status="failed", reason=f"OpenRA settings runtime failed: {error}")
        return result
    _record_state_process_pair_evidence(
        result,
        source=OPENRA_STATE_PAIR_SOURCE,
        algorithm=OPENRA_STATE_PAIR_ALGORITHM,
        initial=before_seed_snapshot,
        final=after_seed_snapshot,
    )
    restore_ms = int((time.perf_counter() - restore_started) * 1000)
    timing = {
        "cold_restore_ms": (
            None
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else restore_ms
        ),
        "warm_restore_ms": (
            before_ready_ms
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else None
        ),
        "speedup": None,
        "fast_restore_ms": None,
        "fast_vs_full_speedup": None,
        "fast_restore_used": False,
        "fast_restore_fallback": False,
        "cache_hit": False,
    }
    after_timing = {
        **timing,
        "warm_restore_ms": None,
        "fast_restore_ms": transition_ms,
        "fast_restore_used": True,
    }
    result.update(
        restore_timing=timing,
        **(
            {"after_timing": after_timing}
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else {}
        ),
        checkpoint_restore_ms=(
            before_ready_ms
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else restore_ms
        ),
        **(
            {"runtime_wall_ms": restore_ms}
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else {}
        ),
        screenshot_before=before_shot,
        screenshot_after=after_shot,
        semantic_status=after_grade.status,
        semantic_detail=after_grade.detail,
    )
    _mark_state_goal_semantic(
        result,
        ok=bool(after_grade.ok),
        status=str(after_grade.status),
        detail=after_grade.detail,
    )
    if not after_grade.ok:
        result.update(
            status="failure",
            reason="OpenRA public-settings Goal semantic validation failed",
            iterations=[
                {
                    "iteration": 1,
                    "result": False,
                    "status": "semantic_failure",
                    "restore_timing": timing,
                    "screenshot_before": before_shot,
                    "screenshot_after": after_shot,
                    "pair_verdict": before_verdict,
                    "start_success": True,
                    "goal_success": False,
                    "e2e_success": False,
                }
            ],
        )
        return result
    if not _informative_screenshot(after_shot):
        _mark_state_goal_visual_incomplete(result)
        result.update(
            status="failure",
            reason=(
                "OpenRA public-settings Goal screenshot is missing or non-informative"
            ),
        )
        return result

    verdict = _run_state_goal_visual_grade(
        result,
        before_verdict=before_verdict,
        live_before=before_shot,
        live_after=after_shot,
        goldens=goldens,
        task=task,
    )
    result["status"] = "success" if result["result"] else "failure"
    result["iterations"] = [
        {
            "iteration": 1,
            "result": result["result"],
            "status": result["status"],
            "restore_timing": timing,
            "screenshot_before": before_shot,
            "screenshot_after": after_shot,
            "pair_verdict": verdict,
            "start_success": result["start_success"],
            "goal_success": result["goal_success"],
            "e2e_success": result["e2e_success"],
        }
    ]
    return result


def _ensure_openra_settings_goldens(
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    *,
    seed: int,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    repo_commit = _repo_git_commit(repo)
    cached = _cached_oracle_goldens(repo_commit, oracle_spec, cache_root=cache_root)
    if cached is not None:
        return cached
    fingerprint = _oracle_fingerprint(repo_commit, oracle_spec)
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    local_dir = report_dir / "oracle-goldens" / fingerprint
    start_settings = openra_target_settings(oracle_spec)
    actions = oracle_spec.get("actions")
    actions = actions if isinstance(actions, list) else []
    goal_settings = apply_openra_setting_actions(start_settings, actions)
    with contextlib.nullcontext():
        with OpenRASettingsSession(
            repo,
            local_dir / "before-runtime",
            settings=start_settings,
            experiment_seed=seed,
        ) as before_runtime:
            before_grade = before_runtime.grade_settings(start_settings)
            before = before_runtime.screenshot(local_dir / "before.png")
        with OpenRASettingsSession(
            repo,
            local_dir / "after-runtime",
            settings=goal_settings,
            experiment_seed=seed,
        ) as after_runtime:
            after_grade = after_runtime.grade_assertions(
                semantic_assertions(oracle_spec) or []
            )
            after = after_runtime.screenshot(local_dir / "after.png")
    if not before_grade.ok or not after_grade.ok:
        raise RuntimeError(
            "OpenRA settings oracle semantic validation failed: "
            f"before={before_grade.detail}, after={after_grade.detail}"
        )
    if not _informative_screenshot(before) or not _informative_screenshot(after):
        raise RuntimeError("OpenRA settings oracle screenshots are non-informative")

    cacheable = bool(repo_commit)
    target_dir = cache_dir if cacheable else local_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_before = target_dir / "golden_before.png"
    target_after = target_dir / "golden_after.png"
    shutil.copyfile(before, target_before)
    shutil.copyfile(after, target_after)
    quality = {
        "before": screenshot_quality(target_before),
        "after": screenshot_quality(target_after),
    }
    consistency = {"source": "openra_public_settings_live_process"}
    _write_oracle_golden_metadata(
        target_dir,
        repo_commit=repo_commit,
        oracle_spec=oracle_spec,
        benchmark="OpenRA",
        oracle_spec_path=oracle_spec_path,
        consistency=consistency,
        quality=quality,
        cacheable=cacheable,
    )
    return OracleGoldens(
        before=str(target_before),
        after=str(target_after),
        fingerprint=fingerprint,
        cache_hit=False,
        cacheable=cacheable,
        consistency=consistency,
        quality=quality,
        artifacts=capture_oracle_golden_artifact_digests(
            target_before, target_after
        ),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def _mark_native_state_runtime_not_started(result: dict[str, Any]) -> None:
    raw_seed = result.get("seed", 0)
    seed = int(raw_seed) if isinstance(raw_seed, int) and not isinstance(raw_seed, bool) else 0
    result["runtime_started"] = False
    result["seed_evidence"] = {
        "schema_version": 1,
        "source": "runtime_not_started",
        "observed": False,
        "algorithm": "not_applicable_native_gui",
        "seed": seed,
        "initial": None,
        "final": None,
    }
    result.pop("prng", None)


def _usable_native_state_model_spec(
    model_spec: Any,
    *,
    task: StateLoopTask,
    oracle_spec: dict[str, Any],
) -> bool:
    """Reject scaffold and cross-benchmark native checkpoints.

    Executability only. Whether the target is the *right* one is decided by the
    live oracle probe at the Start gate, not here, so a wrong target yields a
    measured Start failure instead of a pre-restore rejection.
    """

    if generated_spec_acceptance_errors(
        model_spec, issue=task.issue, benchmark=task.benchmark
    ):
        return False
    if _native_state_benchmark_errors(
        model_spec, oracle_spec=oracle_spec, task=task
    ):
        return False
    target = model_spec.get("target")
    actions = model_spec.get("actions")
    assertions = model_spec.get("assertions")
    return bool(
        isinstance(target, dict)
        and isinstance(target.get("kind"), str)
        and isinstance(model_spec.get("checkpoint"), dict)
        and isinstance(actions, list)
        and all(isinstance(action, dict) for action in actions)
        and isinstance(assertions, list)
        and all(isinstance(assertion, dict) for assertion in assertions)
    )


def _run_blender_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    headless: bool,
    source_repo: Path | None = None,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    """Run Blender arm E in one headed, persistent native desktop process."""

    _ensure_state_stage_metrics(result)
    _mark_native_state_runtime_not_started(result)
    if headless:
        result.update(
            status="unsupported",
            reason="Blender C1 State requires a headed native desktop session",
        )
        return result

    issue = task.issue
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(issue, base, artifact_dir=version_dir / "synthesis-artifacts")
    except Exception as error:
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
        result.update(status="failed", reason=f"config synthesis failed: {error}")
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
    model_spec = getattr(checkpoint, "model_generated_spec", None)
    result["used_public_scaffold"] = bool(
        getattr(checkpoint, "used_public_scaffold", False)
    )
    if not _usable_native_state_model_spec(
        model_spec, task=task, oracle_spec=oracle_spec
    ):
        provider_failure = _checkpoint_provider_failure(checkpoint)
        result.update(
            status="failure",
            reason=(
                "synthesis produced no usable task-compatible model spec; the Blender C1 State "
                "path does not fall back to the public scaffold"
            ),
            injected_spec_source="none",
        )
        if provider_failure is not None:
            result.update(
                status="incomplete",
                incomplete=True,
                reason="checkpoint synthesis provider failure: " + provider_failure,
            )
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result

    generated_spec = _apply_experiment_seed(
        copy.deepcopy(model_spec),
        result.get("seed"),
        benchmark=task.benchmark,
    )
    config_path = version_dir / "spec.yaml"
    config_path.write_text(yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
    result["config_path"] = str(config_path)
    result["injected_spec_source"] = "model_generated_spec"
    result["checkpoint_synthesis_ms"] = synthesis_ms
    result["state_fingerprint"] = state_fingerprint(_repo_git_commit(repo), generated_spec)
    result["generated_spec_hash"] = generated_spec_hash(generated_spec)

    golden_started = time.perf_counter()
    try:
        goldens = _registered_blender_goldens(
            repo,
            oracle_spec,
            oracle_spec_path,
            task_dir / "oracle",
        )
        result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"oracle golden generation failed: {error}"
        result["grader_ms"] = int(
            (time.perf_counter() - golden_started) * 1000
        )
        return result
    result["grader_ms"] = int((time.perf_counter() - golden_started) * 1000)

    runtime_started = False
    runtime_bootstrap_started = time.perf_counter()
    try:
        runtime_kwargs: dict[str, Any] = {}
        if source_repo is not None:
            # The APFS sandbox contains the source checkout only.  Blender's
            # trusted built application is a sibling of the original checkout,
            # so resolve it there and bind the exact executable into the
            # sandboxed State session (the GUI Agent path follows the same
            # provenance rule).
            trusted_source_repo = Path(source_repo).expanduser().resolve()
            runtime_kwargs["executable"] = find_blender_executable(
                trusted_source_repo
            )
            result["trusted_source_repo"] = str(trusted_source_repo)
            result["runtime_executable"] = str(runtime_kwargs["executable"])
        with BlenderC1DesktopSession(
            repo, task_dir / "session", **runtime_kwargs
        ) as runtime:
            runtime_started = True
            runtime_bootstrap_ms = int(
                (time.perf_counter() - runtime_bootstrap_started) * 1000
            )
            result.update(
                runtime_started=True,
                runtime_bootstrap_ms=runtime_bootstrap_ms,
                session_bootstrap_ms=runtime_bootstrap_ms,
            )

            entry_probe = runtime.probe_liveness()
            _validate_native_state_liveness(
                entry_probe,
                expected_source=BLENDER_SEMANTIC_SOURCE,
                benchmark="Blender",
            )
            entry_seed = runtime.seed_evidence(
                int(result.get("seed", 0)),
                entry_probe,
                task.id,
                "entry",
            )
            _bind_native_state_seed_evidence(
                result,
                runtime,
                initial=entry_seed,
                final=None,
                initial_probe=entry_probe,
                final_probe=None,
                initial_stage="entry",
            )

            restore_started = time.perf_counter()
            restore_report = runtime.restore_target(generated_spec)
            _validate_native_state_restore(
                restore_report,
                expected_source=BLENDER_SEMANTIC_SOURCE,
                benchmark="Blender",
            )
            restore_ms = int((time.perf_counter() - restore_started) * 1000)
            if restore_report.get("status") == "failure":
                return _native_state_candidate_restore_failure(
                    result,
                    restore_report,
                    benchmark="Blender",
                    restore_ms=restore_ms,
                )
            before_probe = runtime.probe_start(generated_spec)
            before_ok = _validate_blender_state_probe(
                before_probe,
                label="start",
                require_transition=False,
            )
            oracle_probe = runtime.probe_start(oracle_spec)
            oracle_probe_ok = _validate_blender_state_probe(
                oracle_probe,
                label="oracle-start",
                require_transition=False,
            )
            before_desktop = runtime.screenshot(
                task_dir / "candidate-before-desktop.png"
            )
            before_proof = runtime.render_proof(
                task_dir / "candidate-before-render.png",
                canonical_spec=generated_spec,
            )
            initial_seed = runtime.seed_evidence(
                int(result.get("seed", 0)),
                before_probe,
                task.id,
                "restored",
            )
            _bind_native_state_seed_evidence(
                result,
                runtime,
                initial=initial_seed,
                final=None,
                initial_probe=before_probe,
                final_probe=None,
            )

            restore_timing = {
                "cold_restore_ms": None,
                "warm_restore_ms": restore_ms,
                "speedup": None,
                "fast_restore_ms": None,
                "fast_vs_full_speedup": None,
                "fast_restore_used": False,
                "fast_restore_fallback": False,
                "cache_hit": False,
                "runtime_exit_code": None,
            }
            result.update(
                restore_timing=restore_timing,
                checkpoint_restore_ms=restore_ms,
                screenshot_before=before_proof,
                desktop_screenshot_before=before_desktop,
                initial_state_fingerprint=_image_fingerprint(before_proof),
                restore_report=restore_report,
            )
            semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
                restore_ok=before_ok,
                restore_status=str(before_probe.get("status") or "failure"),
                restore_detail=before_probe,
                oracle_grade=_oracle_probe_grade(
                    oracle_probe,
                    ok=oracle_probe_ok,
                    status=str(oracle_probe.get("status") or "validate_failed"),
                ),
            )
            _mark_state_start_semantic(
                result,
                ok=semantic_ok,
                status=semantic_status,
                detail=semantic_detail,
            )
            before_ok = semantic_ok
            before_visual_informative = _informative_screenshot(before_proof)
            if not before_visual_informative or not before_ok:
                if not before_ok:
                    _freeze_pair_start_semantic_failure(
                        result,
                        task=task,
                        accepted_spec=generated_spec,
                        synthesis_agent=agent,
                        live_before=before_proof,
                    )
                if before_ok and not before_visual_informative:
                    _mark_state_start_visual_incomplete(result)
                result.update(
                    status="failure",
                    reason=(
                        "Blender before-state render proof is missing or non-informative"
                        if not before_visual_informative
                        else (
                            "Blender restored Start semantic validation failed "
                            f"({semantic_status})"
                        )
                    ),
                    iterations=[
                        {
                            "iteration": 1,
                            "result": False,
                            "status": (
                                "restore_failure"
                                if not before_visual_informative
                                else "semantic_failure"
                            ),
                            "restore_timing": restore_timing,
                            "screenshot_before": before_proof,
                            "semantic_start": before_probe,
                            "start_success": False,
                            "goal_success": False,
                            "e2e_success": False,
                        }
                    ],
                )
                return result
            start_ok, before_verdict = _run_state_start_visual_gate(
                result,
                live_before=before_proof,
                goldens=goldens,
                task=task,
                runtime=runtime,
                accepted_spec=generated_spec,
                synthesis_agent=agent,
            )
            if not start_ok:
                result.update(
                    status="failure",
                    reason="Blender restored Start visual validation failed",
                    iterations=[
                        {
                            "iteration": 1,
                            "result": False,
                            "status": "start_gate_failure",
                            "restore_timing": restore_timing,
                            "screenshot_before": before_proof,
                            "semantic_start": before_probe,
                            "pair_verdict": before_verdict,
                            "start_success": False,
                            "goal_success": False,
                            "e2e_success": False,
                        }
                    ],
                )
                return result

            transition_spec = _plan_minimal_smoke_transition(
                result,
                planner=minimal_planner,
                base=base,
                task=task,
                generated_spec=generated_spec,
                task_dir=version_dir,
                seed=int(result.get("seed", 0)),
            )
            if transition_spec is None:
                return result
            transition_started = time.perf_counter()
            after_probe: dict[str, Any] | None = None
            after_desktop: str | None = None
            after_proof: str | None = None
            after_ok = False
            if before_ok:
                grader_spec = copy.deepcopy(transition_spec)
                grader_spec["assertions"] = copy.deepcopy(
                    semantic_assertions(oracle_spec) or []
                )
                _mark_transition_action_execution_started(result)
                after_probe = runtime.execute_state_transition(grader_spec)
                after_ok = _validate_blender_state_probe(
                    after_probe,
                    label="goal",
                    require_transition=True,
                )
                after_desktop = runtime.screenshot(
                    task_dir / "candidate-after-desktop.png"
                )
                after_proof = runtime.render_proof(
                    task_dir / "candidate-after-render.png",
                    canonical_spec=transition_spec,
                )
            transition_ms = int((time.perf_counter() - transition_started) * 1000)
            final_seed = (
                runtime.seed_evidence(
                    int(result.get("seed", 0)),
                    after_probe,
                    task.id,
                    "goal",
                )
                if isinstance(after_probe, dict)
                else None
            )
            _bind_native_state_seed_evidence(
                result,
                runtime,
                initial=initial_seed,
                final=final_seed,
                initial_probe=before_probe,
                final_probe=after_probe,
            )
    except C1DesktopRuntimeUnsupported as error:
        result.update(
            status="failure" if runtime_started else "unsupported",
            reason=(
                f"Blender candidate checkpoint is unsupported: {error}"
                if runtime_started
                else f"Blender native desktop runtime is unavailable: {error}"
            ),
            runtime_started=runtime_started,
        )
        return result
    except Exception as error:
        result.update(
            status="failed",
            reason=f"Blender C1 State runtime failed: {error}",
            runtime_started=runtime_started,
        )
        return result

    after_timing = {
        **restore_timing,
        "warm_restore_ms": None,
        "fast_restore_ms": transition_ms,
        "fast_restore_used": True,
    }
    result.update(
        restore_timing=restore_timing,
        after_timing=after_timing,
        checkpoint_restore_ms=restore_ms,
        screenshot_before=before_proof,
        screenshot_after=after_proof,
        desktop_screenshot_before=before_desktop,
        desktop_screenshot_after=after_desktop,
        initial_state_fingerprint=_image_fingerprint(before_proof),
        restore_report=restore_report,
        before_semantic_ok=before_ok,
        before_semantic_status=str(before_probe.get("status") or "failure"),
        before_semantic_detail=before_probe,
        after_semantic_ok=after_ok,
        semantic_status=(
            str(after_probe.get("status") or "failure")
            if isinstance(after_probe, dict)
            else "not_evaluated"
        ),
        after_semantic_status=(
            str(after_probe.get("status") or "failure")
            if isinstance(after_probe, dict)
            else "not_evaluated"
        ),
        semantic_detail=after_probe,
        after_semantic_detail=after_probe,
        semantic_probe_source=BLENDER_SEMANTIC_SOURCE,
        registered_transition=after_probe,
        runtime_report=after_probe,
        runtime_access_methods={
            "semantic": BLENDER_SEMANTIC_SOURCE,
            "visual": "blender_live_render_proof_v1",
            "desktop_visual": "macos_full_desktop",
            "transition": BLENDER_STATE_TRANSITION_BACKEND,
        },
        restore_actions_executed=False,
        state_transition_backend=(
            after_probe.get("state_transition_backend")
            if isinstance(after_probe, dict)
            else None
        ),
        scripted_transition_executed=isinstance(after_probe, dict),
    )
    _mark_state_goal_semantic(
        result,
        ok=after_ok,
        status=(
            str(after_probe.get("status") or "failure")
            if isinstance(after_probe, dict)
            else "failure"
        ),
        detail=after_probe,
    )
    if not _informative_screenshot(after_proof):
        if after_ok:
            _mark_state_goal_visual_incomplete(result)
        result["status"] = "failure"
        result["reason"] = "Blender after-state render proof is missing or non-informative"
        result["iterations"] = [
            {
                "iteration": 1,
                "result": False,
                "status": "smoke_failure",
                "restore_timing": restore_timing,
                "after_timing": after_timing,
                "screenshot_before": before_proof,
                "screenshot_after": after_proof,
                "semantic_start": before_probe,
                "semantic_goal": after_probe,
            }
        ]
        return result
    if not after_ok:
        result.update(
            status="failure",
            result=False,
            incomplete=False,
            treatment_failure=True,
            model_failure_stage="transition",
            reason="Blender goal-state semantic validation failed",
            iterations=[
                {
                    "iteration": 1,
                    "result": False,
                    "status": "semantic_failure",
                    "restore_timing": restore_timing,
                    "after_timing": after_timing,
                    "screenshot_before": before_proof,
                    "screenshot_after": after_proof,
                    "semantic_start": before_probe,
                    "semantic_goal": after_probe,
                    "pair_verdict": before_verdict,
                    "start_success": True,
                    "goal_success": False,
                    "e2e_success": False,
                }
            ],
        )
        return result

    verdict = _run_state_goal_visual_grade(
        result,
        before_verdict=before_verdict,
        live_before=before_proof,
        live_after=after_proof,
        goldens=goldens,
        task=task,
    )
    result["status"] = "success" if result["result"] else "failure"
    result["iterations"] = [
        {
            "iteration": 1,
            "result": result["result"],
            "status": result["status"],
            "restore_timing": restore_timing,
            "after_timing": after_timing,
            "screenshot_before": before_proof,
            "screenshot_after": after_proof,
            "semantic_start": before_probe,
            "semantic_goal": after_probe,
            "registered_transition": after_probe,
            "pair_verdict": verdict,
            "start_success": result["start_success"],
            "goal_success": result["goal_success"],
            "e2e_success": result["e2e_success"],
        }
    ]
    return result


def _registered_blender_goldens(
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    *,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    return _registered_desktop_state_goldens(
        repo,
        oracle_spec,
        oracle_spec_path,
        report_dir,
        benchmark="Blender",
        cache_root=cache_root,
    )


def _run_libreoffice_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    headless: bool,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    """Run arm E in one headed, persistent LibreOffice desktop session.

    State management may restore the synthesized target and execute its allow-listed
    transition programmatically.  The candidate is nevertheless graded against live
    UNO observations and full-desktop captures from the same native process.  This
    path must never fall back to the legacy batch adapter.
    """

    _ensure_state_stage_metrics(result)
    _mark_native_state_runtime_not_started(result)
    if headless:
        result.update(
            status="unsupported",
            reason="LibreOffice C1 State requires a headed native desktop session",
        )
        return result

    issue = task.issue
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(
            issue, base, artifact_dir=version_dir / "synthesis-artifacts"
        )
    except Exception as error:
        result.update(
            status="failed",
            reason=f"config synthesis failed: {error}",
            checkpoint_synthesis_ms=int(
                (time.perf_counter() - synthesis_started) * 1000
            ),
        )
        return result
    result["checkpoint_synthesis_ms"] = int(
        (time.perf_counter() - synthesis_started) * 1000
    )

    model_spec = getattr(checkpoint, "model_generated_spec", None)
    result["used_public_scaffold"] = bool(
        getattr(checkpoint, "used_public_scaffold", False)
    )
    if not _usable_native_state_model_spec(
        model_spec, task=task, oracle_spec=oracle_spec
    ):
        provider_failure = _checkpoint_provider_failure(checkpoint)
        result.update(
            status="failure",
            reason=(
                "synthesis produced no usable task-compatible model spec; the LibreOffice C1 State "
                "path does not fall back to the public scaffold"
            ),
            injected_spec_source="none",
        )
        if provider_failure is not None:
            result.update(
                status="incomplete",
                incomplete=True,
                reason="checkpoint synthesis provider failure: " + provider_failure,
            )
        _set_state_method_timing(
            result, checkpoint_synthesis_ms=result["checkpoint_synthesis_ms"]
        )
        return result
    generated_spec = _apply_experiment_seed(
        copy.deepcopy(model_spec),
        result.get("seed"),
        benchmark=task.benchmark,
    )
    config_path = version_dir / "spec.yaml"
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result["config_path"] = str(config_path)
    result["injected_spec_source"] = "model_generated_spec"
    result["state_fingerprint"] = state_fingerprint(
        _repo_git_commit(repo), generated_spec
    )
    result["generated_spec_hash"] = generated_spec_hash(generated_spec)

    golden_started = time.perf_counter()
    try:
        goldens = _registered_libreoffice_goldens(
            repo,
            oracle_spec,
            oracle_spec_path,
            task_dir / "oracle",
        )
        result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"oracle golden generation failed: {error}"
        result["grader_ms"] = int(
            (time.perf_counter() - golden_started) * 1000
        )
        return result
    result["grader_ms"] = int((time.perf_counter() - golden_started) * 1000)

    runtime_started = False
    runtime_bootstrap_started = time.perf_counter()
    try:
        with LibreOfficeC1DesktopSession(repo, task_dir / "session") as runtime:
            runtime_started = True
            runtime_bootstrap_ms = int(
                (time.perf_counter() - runtime_bootstrap_started) * 1000
            )
            result["runtime_bootstrap_ms"] = runtime_bootstrap_ms
            result["session_bootstrap_ms"] = runtime_bootstrap_ms
            result["runtime_started"] = True

            entry_probe = runtime.probe_liveness()
            _validate_native_state_liveness(
                entry_probe,
                expected_source=LIBREOFFICE_SEMANTIC_SOURCE,
                benchmark="LibreOffice",
            )
            entry_seed = runtime.seed_evidence(
                int(result.get("seed", 0)),
                entry_probe,
                task.id,
                "entry",
            )
            _bind_native_state_seed_evidence(
                result,
                runtime,
                initial=entry_seed,
                final=None,
                initial_probe=entry_probe,
                final_probe=None,
                initial_stage="entry",
            )

            restore_started = time.perf_counter()
            restore_report = runtime.restore_target(generated_spec)
            _validate_libreoffice_state_restore(restore_report)
            restore_ms = int((time.perf_counter() - restore_started) * 1000)
            if restore_report.get("status") == "failure":
                return _native_state_candidate_restore_failure(
                    result,
                    restore_report,
                    benchmark="LibreOffice",
                    restore_ms=restore_ms,
                )
            before_probe = runtime.probe_start(generated_spec)
            before_ok = _validate_libreoffice_state_probe(
                before_probe,
                label="start",
                require_persistence=False,
            )
            oracle_probe = runtime.probe_start(oracle_spec)
            oracle_probe_ok = _validate_libreoffice_state_probe(
                oracle_probe,
                label="oracle-start",
                require_persistence=False,
            )
            # The registered visual oracle owns the camera/view normalization.
            # Apply the same active-sheet/slide policy to the live candidate
            # before capture; otherwise a semantically correct workbook can be
            # rejected merely because restore left a different tab selected.
            before_visual_preparation = runtime.prepare_visual_capture(
                oracle_spec, "before"
            )
            before_shot = runtime.screenshot(task_dir / "candidate-before.png")
            initial_seed = runtime.seed_evidence(
                int(result.get("seed", 0)),
                before_probe,
                task.id,
                "restored",
            )
            _bind_native_state_seed_evidence(
                result,
                runtime,
                initial=initial_seed,
                final=None,
                initial_probe=before_probe,
                final_probe=None,
            )

            restore_timing = {
                "cold_restore_ms": None,
                "warm_restore_ms": restore_ms,
                "speedup": None,
                "fast_restore_ms": None,
                "fast_vs_full_speedup": None,
                "fast_restore_used": False,
                "fast_restore_fallback": False,
                "cache_hit": False,
                "runtime_exit_code": None,
            }
            result.update(
                restore_timing=restore_timing,
                checkpoint_restore_ms=restore_ms,
                screenshot_before=before_shot,
                initial_state_fingerprint=_image_fingerprint(before_shot),
                restore_report=restore_report,
                before_visual_preparation=before_visual_preparation,
            )
            semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
                restore_ok=before_ok,
                restore_status=str(before_probe.get("status") or "failure"),
                restore_detail=before_probe,
                oracle_grade=_oracle_probe_grade(
                    oracle_probe,
                    ok=oracle_probe_ok,
                    status=str(oracle_probe.get("status") or "validate_failed"),
                ),
            )
            _mark_state_start_semantic(
                result,
                ok=semantic_ok,
                status=semantic_status,
                detail=semantic_detail,
            )
            before_ok = semantic_ok
            before_visual_informative = _informative_screenshot(before_shot)
            if not before_visual_informative or not before_ok:
                if not before_ok:
                    _freeze_pair_start_semantic_failure(
                        result,
                        task=task,
                        accepted_spec=generated_spec,
                        synthesis_agent=agent,
                        live_before=before_shot,
                    )
                if before_ok and not before_visual_informative:
                    _mark_state_start_visual_incomplete(result)
                result.update(
                    status="failure",
                    reason=(
                        "LibreOffice before-state screenshot is missing or non-informative"
                        if not before_visual_informative
                        else (
                            "LibreOffice restored Start semantic validation failed "
                            f"({semantic_status})"
                        )
                    ),
                    iterations=[
                        {
                            "iteration": 1,
                            "result": False,
                            "status": (
                                "restore_failure"
                                if not before_visual_informative
                                else "semantic_failure"
                            ),
                            "restore_timing": restore_timing,
                            "screenshot_before": before_shot,
                            "semantic_start": before_probe,
                            "start_success": False,
                            "goal_success": False,
                            "e2e_success": False,
                        }
                    ],
                )
                return result
            start_ok, before_verdict = _run_state_start_visual_gate(
                result,
                live_before=before_shot,
                goldens=goldens,
                task=task,
                runtime=runtime,
                accepted_spec=generated_spec,
                synthesis_agent=agent,
            )
            if not start_ok:
                result.update(
                    status="failure",
                    reason="LibreOffice restored Start visual validation failed",
                    iterations=[
                        {
                            "iteration": 1,
                            "result": False,
                            "status": "start_gate_failure",
                            "restore_timing": restore_timing,
                            "screenshot_before": before_shot,
                            "semantic_start": before_probe,
                            "pair_verdict": before_verdict,
                            "start_success": False,
                            "goal_success": False,
                            "e2e_success": False,
                        }
                    ],
                )
                return result

            transition_spec = _plan_minimal_smoke_transition(
                result,
                planner=minimal_planner,
                base=base,
                task=task,
                generated_spec=generated_spec,
                task_dir=version_dir,
                seed=int(result.get("seed", 0)),
            )
            if transition_spec is None:
                return result
            transition_started = time.perf_counter()
            after_probe: dict[str, Any] | None = None
            after_shot: str | None = None
            after_visual_preparation: dict[str, Any] | None = None
            after_ok = False
            if before_ok:
                # Model transition, registered-oracle assertions.
                grader_spec = copy.deepcopy(transition_spec)
                grader_spec["assertions"] = copy.deepcopy(
                    semantic_assertions(oracle_spec) or []
                )
                _mark_transition_action_execution_started(result)
                after_probe = runtime.execute_state_transition(grader_spec)
                after_ok = _validate_libreoffice_state_probe(
                    after_probe,
                    label="goal",
                    require_persistence=True,
                    start_probe=before_probe,
                )
                after_visual_preparation = runtime.prepare_visual_capture(
                    oracle_spec, "after"
                )
                after_shot = runtime.screenshot(task_dir / "candidate-after.png")
            transition_ms = int((time.perf_counter() - transition_started) * 1000)
            final_seed = (
                runtime.seed_evidence(
                    int(result.get("seed", 0)),
                    after_probe,
                    task.id,
                    "goal",
                )
                if isinstance(after_probe, dict)
                else None
            )
            _bind_native_state_seed_evidence(
                result,
                runtime,
                initial=initial_seed,
                final=final_seed,
                initial_probe=before_probe,
                final_probe=after_probe,
            )
    except C1DesktopRuntimeUnsupported as error:
        result.update(
            status="failure" if runtime_started else "unsupported",
            reason=(
                f"LibreOffice candidate checkpoint is unsupported: {error}"
                if runtime_started
                else f"LibreOffice native desktop runtime is unavailable: {error}"
            ),
            runtime_started=runtime_started,
        )
        return result
    except Exception as error:
        result.update(
            status="failed",
            reason=f"LibreOffice C1 State runtime failed: {error}",
            runtime_started=runtime_started,
        )
        return result

    after_timing = {
        **restore_timing,
        "warm_restore_ms": None,
        "fast_restore_ms": transition_ms,
        "fast_restore_used": True,
    }
    result.update(
        restore_timing=restore_timing,
        after_timing=after_timing,
        checkpoint_restore_ms=restore_ms,
        screenshot_before=before_shot,
        screenshot_after=after_shot,
        initial_state_fingerprint=_image_fingerprint(before_shot),
        restore_report=restore_report,
        before_visual_preparation=before_visual_preparation,
        after_visual_preparation=after_visual_preparation,
        before_semantic_ok=before_ok,
        before_semantic_status=str(before_probe.get("status") or "failure"),
        before_semantic_detail=before_probe,
        after_semantic_ok=after_ok,
        semantic_status=(
            str(after_probe.get("status") or "failure")
            if isinstance(after_probe, dict)
            else "not_evaluated"
        ),
        after_semantic_status=(
            str(after_probe.get("status") or "failure")
            if isinstance(after_probe, dict)
            else "not_evaluated"
        ),
        semantic_detail=after_probe,
        after_semantic_detail=after_probe,
        semantic_probe_source=LIBREOFFICE_SEMANTIC_SOURCE,
        registered_transition=after_probe,
        runtime_report=after_probe,
        runtime_access_methods={
            "semantic": LIBREOFFICE_SEMANTIC_SOURCE,
            "visual": "macos_full_desktop",
            "transition": LIBREOFFICE_STATE_TRANSITION_BACKEND,
        },
        restore_actions_executed=False,
        state_transition_backend=(
            after_probe.get("state_transition_backend")
            if isinstance(after_probe, dict)
            else None
        ),
        scripted_transition_executed=isinstance(after_probe, dict),
    )
    _mark_state_goal_semantic(
        result,
        ok=after_ok,
        status=(
            str(after_probe.get("status") or "failure")
            if isinstance(after_probe, dict)
            else "failure"
        ),
        detail=after_probe,
    )
    if not _informative_screenshot(after_shot):
        if after_ok:
            _mark_state_goal_visual_incomplete(result)
        result["status"] = "failure"
        result["reason"] = "LibreOffice after-state screenshot is missing or non-informative"
        result["iterations"] = [
            {
                "iteration": 1,
                "result": False,
                "status": "smoke_failure",
                "restore_timing": restore_timing,
                "after_timing": after_timing,
                "screenshot_before": before_shot,
                "screenshot_after": after_shot,
                "semantic_start": before_probe,
                "semantic_goal": after_probe,
            }
        ]
        return result
    if not after_ok:
        result.update(
            status="failure",
            result=False,
            incomplete=False,
            treatment_failure=True,
            model_failure_stage="transition",
            reason="LibreOffice persisted goal-state semantic validation failed",
            iterations=[
                {
                    "iteration": 1,
                    "result": False,
                    "status": "semantic_failure",
                    "restore_timing": restore_timing,
                    "after_timing": after_timing,
                    "screenshot_before": before_shot,
                    "screenshot_after": after_shot,
                    "semantic_start": before_probe,
                    "semantic_goal": after_probe,
                    "pair_verdict": before_verdict,
                    "start_success": True,
                    "goal_success": False,
                    "e2e_success": False,
                }
            ],
        )
        return result

    verdict = _run_state_goal_visual_grade(
        result,
        before_verdict=before_verdict,
        live_before=before_shot,
        live_after=after_shot,
        goldens=goldens,
        task=task,
    )
    result["status"] = "success" if result["result"] else "failure"
    result["iterations"] = [
        {
            "iteration": 1,
            "result": result["result"],
            "status": result["status"],
            "restore_timing": restore_timing,
            "after_timing": after_timing,
            "screenshot_before": before_shot,
            "screenshot_after": after_shot,
            "semantic_start": before_probe,
            "semantic_goal": after_probe,
            "registered_transition": after_probe,
            "pair_verdict": verdict,
            "start_success": result["start_success"],
            "goal_success": result["goal_success"],
            "e2e_success": result["e2e_success"],
        }
    ]
    return result


def _validate_native_state_restore(
    report: Any,
    *,
    expected_source: str,
    benchmark: str,
) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise C1DesktopRuntimeError(
            f"{benchmark} target-only restore returned a non-object"
        )
    checks = report.get("checks")
    errors = report.get("errors", [])
    target_only = [
        row
        for row in checks if isinstance(row, dict) and row.get("name") == "restore.target_only"
    ] if isinstance(checks, list) else []
    status = report.get("status")
    if (
        status not in {"success", "failure"}
        or report.get("source") != expected_source
        or report.get("live") is not True
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row.get("name")
            or type(row.get("ok")) is not bool
            for row in checks
        )
        or len(target_only) != 1
        or not isinstance(errors, list)
        or any(not isinstance(error, str) for error in errors)
    ):
        raise C1DesktopRuntimeError(
            f"{benchmark} target-only restore evidence is malformed"
        )
    if errors:
        raise C1DesktopRuntimeError(
            f"{benchmark} target-only restore reported runtime errors: {errors}"
        )
    # A live bridge may reject a malformed model-generated target before it can
    # restore anything; that valid treatment outcome is represented by the
    # required restore.target_only check itself being false.  Successful reports
    # still require every check (including restore.target_only) to pass.
    checks_passed = all(row.get("ok") is True for row in checks)
    if (status == "success") is not checks_passed:
        raise C1DesktopRuntimeError(
            f"{benchmark} target-only restore status/checks are inconsistent"
        )
    return report


def _native_state_candidate_restore_failure(
    result: dict[str, Any],
    restore_report: dict[str, Any],
    *,
    benchmark: str,
    restore_ms: int,
) -> dict[str, Any]:
    """Record a valid model-generated checkpoint rejection as a fair zero.

    A live native backend can correctly reject a semantically inconsistent model
    spec.  That is treatment behavior, not missing evidence or infrastructure
    failure, and must not abort the remaining invocation.
    """

    timing = {
        "cold_restore_ms": None,
        "warm_restore_ms": max(0, int(restore_ms)),
        "speedup": None,
        "fast_restore_ms": None,
        "fast_vs_full_speedup": None,
        "fast_restore_used": False,
        "fast_restore_fallback": False,
        "cache_hit": False,
        "runtime_exit_code": None,
    }
    result.update(
        status="failure",
        result=False,
        reason=f"{benchmark} candidate target-only restore semantic validation failed",
        treatment_failure=True,
        model_failure_stage="restore",
        start_construction_status="restore_failed",
        start_gate_status="not_evaluated_start_construction_failed",
        goal_evaluation_status="not_evaluated_start_construction_failed",
        visual_grading_started=False,
        restore_report=restore_report,
        restore_timing=timing,
        checkpoint_restore_ms=max(0, int(restore_ms)),
        before_semantic_ok=False,
        iterations=[],
    )
    _set_state_method_timing(result)
    return result


def _validate_native_state_liveness(
    report: Any,
    *,
    expected_source: str,
    benchmark: str,
) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise C1DesktopRuntimeError(
            f"{benchmark} native liveness probe returned a non-object"
        )
    checks = report.get("checks")
    errors = report.get("errors")
    if (
        report.get("status") != "success"
        or report.get("source") != expected_source
        or report.get("live") is not True
        or report.get("passed") is not True
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row.get("name")
            or type(row.get("ok")) is not bool
            or row.get("ok") is not True
            for row in checks
        )
        or not isinstance(errors, list)
        or any(not isinstance(error, str) for error in errors)
        or errors
    ):
        raise C1DesktopRuntimeError(
            f"{benchmark} native liveness evidence is malformed or unsuccessful"
        )
    return report


def _validate_libreoffice_state_restore(report: Any) -> dict[str, Any]:
    return _validate_native_state_restore(
        report,
        expected_source=LIBREOFFICE_SEMANTIC_SOURCE,
        benchmark="LibreOffice",
    )


def _validate_blender_state_probe(
    report: Any,
    *,
    label: str,
    require_transition: bool,
) -> bool:
    if not isinstance(report, dict):
        raise C1DesktopRuntimeError(
            f"Blender {label} semantic report is not a mapping"
        )
    checks = report.get("checks")
    errors = report.get("errors", [])
    if (
        report.get("source") != BLENDER_SEMANTIC_SOURCE
        or report.get("live") is not True
        or report.get("status") not in {"success", "failure"}
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row.get("name")
            or type(row.get("ok")) is not bool
            for row in checks
        )
        or not isinstance(errors, list)
        or any(not isinstance(error, str) for error in errors)
        or type(report.get("passed")) is not bool
    ):
        raise C1DesktopRuntimeError(
            f"Blender {label} semantic evidence is malformed"
        )
    passed = semantic_probe_passed(
        report, expected_source=BLENDER_SEMANTIC_SOURCE
    ) and not errors
    if require_transition:
        action_checks = report.get("state_action_checks")
        transition_checks = [
            row
            for row in action_checks
            if isinstance(row, dict)
            and row.get("name") == "transition.actions_applied"
        ] if isinstance(action_checks, list) else []
        if (
            report.get("state_transition_backend")
            != BLENDER_STATE_TRANSITION_BACKEND
            or not isinstance(action_checks, list)
            or not action_checks
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not row.get("name")
                or type(row.get("ok")) is not bool
                for row in action_checks
            )
            or len(transition_checks) != 1
        ):
            raise C1DesktopRuntimeError(
                "Blender goal report lacks registered live state-transition evidence"
            )
        passed = passed and all(row["ok"] is True for row in action_checks)
    if report.get("passed") is not passed:
        raise C1DesktopRuntimeError(
            f"Blender {label} semantic passed flag is inconsistent"
        )
    return passed


def _validate_libreoffice_state_probe(
    report: Any,
    *,
    label: str,
    require_persistence: bool,
    start_probe: dict[str, Any] | None = None,
) -> bool:
    if not isinstance(report, dict):
        raise C1DesktopRuntimeError(
            f"LibreOffice {label} semantic report is not a mapping"
        )
    checks = report.get("checks")
    errors = report.get("errors", [])
    if (
        report.get("source") != LIBREOFFICE_SEMANTIC_SOURCE
        or report.get("live") is not True
        or report.get("status") not in {"success", "failure"}
        or not isinstance(checks, list)
        or not checks
        or any(
            not isinstance(row, dict)
            or not isinstance(row.get("name"), str)
            or not row.get("name")
            or type(row.get("ok")) is not bool
            for row in checks
        )
        or not isinstance(errors, list)
        or any(not isinstance(error, str) for error in errors)
        or type(report.get("passed")) is not bool
        or not isinstance(report.get("document_identity"), str)
        or not report.get("document_identity")
        or not isinstance(report.get("session_identity"), str)
        or not report.get("session_identity")
        or not isinstance(report.get("document_url"), str)
        or type(report.get("document_modified")) is not bool
    ):
        raise C1DesktopRuntimeError(
            f"LibreOffice {label} semantic evidence is malformed"
        )

    passed = semantic_probe_passed(
        report, expected_source=LIBREOFFICE_SEMANTIC_SOURCE
    ) and not errors
    if require_persistence:
        names = {str(row["name"]) for row in checks}
        action_checks = report.get("state_action_checks")
        transition_stage = report.get("state_transition_stage")
        transition_checks = [
            row
            for row in action_checks
            if isinstance(row, dict)
            and row.get("name") == "transition.actions_applied"
        ] if isinstance(action_checks, list) else []
        if (
            report.get("state_transition_backend")
            != LIBREOFFICE_STATE_TRANSITION_BACKEND
            or not isinstance(action_checks, list)
            or not action_checks
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("name"), str)
                or not row.get("name")
                or type(row.get("ok")) is not bool
                for row in action_checks
            )
            or transition_stage not in {"actions", "goal"}
            or len(transition_checks) != 1
        ):
            raise C1DesktopRuntimeError(
                "LibreOffice goal report lacks native save/reopen persistence evidence"
            )
        passed = passed and all(row["ok"] is True for row in action_checks)
        if transition_stage == "actions":
            if report.get("status") != "failure" or passed:
                raise C1DesktopRuntimeError(
                    "LibreOffice failed mutation report is status-inconsistent"
                )
            if report.get("passed") is not False:
                raise C1DesktopRuntimeError(
                    "LibreOffice failed mutation passed flag is inconsistent"
                )
            return False
        if (
            not LIBREOFFICE_STATE_PERSISTENCE_CHECKS.issubset(names)
            or not isinstance(report.get("document_path"), str)
            or not report.get("document_path")
            or not report.get("document_url")
            or report.get("document_modified") is not False
        ):
            raise C1DesktopRuntimeError(
                "LibreOffice goal report lacks native save/reopen persistence evidence"
            )
        if isinstance(start_probe, dict):
            if report.get("session_identity") != start_probe.get("session_identity"):
                raise C1DesktopRuntimeError(
                    "LibreOffice state transition changed native office session"
                )
            if report.get("document_identity") == start_probe.get(
                "document_identity"
            ):
                raise C1DesktopRuntimeError(
                    "LibreOffice state transition did not prove a document reopen"
                )
    if report.get("passed") is not passed:
        raise C1DesktopRuntimeError(
            f"LibreOffice {label} semantic passed flag is inconsistent"
        )
    return passed


def _bind_native_state_seed_evidence(
    result: dict[str, Any],
    runtime: Any,
    *,
    initial: Any,
    final: Any,
    initial_probe: dict[str, Any],
    final_probe: dict[str, Any] | None,
    initial_stage: str = "restored",
) -> None:
    if not isinstance(initial, dict):
        raise C1DesktopRuntimeError(
            "native State initial seed evidence is missing"
        )
    snapshots = [(initial, initial_stage, initial_probe)]
    if isinstance(final, dict):
        if not isinstance(final_probe, dict):
            raise C1DesktopRuntimeError(
                "native State final seed evidence lacks its live semantic probe"
            )
        snapshots.append((final, "goal", final_probe))
    seed = int(result.get("seed", 0))
    task_id = str(result.get("task_id") or "")
    expected_source = str(runtime.seed_evidence_source)
    expected_semantic_source = str(runtime.semantic_source)
    expected_benchmark = str(runtime.benchmark)
    process_id = initial.get("process_id")
    if (
        not isinstance(process_id, int)
        or process_id <= 0
        or any(
            snapshot.get("observed") is not True
            or snapshot.get("source") != expected_source
            or snapshot.get("algorithm") != "not_applicable_native_gui"
            or snapshot.get("seed") != seed
            or snapshot.get("process_id") != process_id
            or snapshot.get("process_alive_at_observation") is not True
            or snapshot.get("task_id") != task_id
            or snapshot.get("semantic_stage") != stage
            or snapshot.get("semantic_source") != expected_semantic_source
            or snapshot.get("semantic_observed") is not True
            or snapshot.get("semantic_observation") != probe
            or str(snapshot.get("benchmark") or "").lower()
            != expected_benchmark.lower()
            for snapshot, stage, probe in snapshots
        )
    ):
        raise C1DesktopRuntimeError(
            "native State seed evidence is not bound to the exact task/seed/live process"
        )
    result["seed_evidence"] = {
        "schema_version": 1,
        "source": expected_source,
        "observed": True,
        "algorithm": "not_applicable_native_gui",
        "seed": seed,
        "initial": initial,
        "final": final,
    }
    result["prng"] = dict(final if isinstance(final, dict) else initial)


def _committed_registered_desktop_visual_pair(
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    *,
    benchmark: str,
) -> tuple[dict[str, Path], str]:
    visual = visual_oracle(oracle_spec)
    if not isinstance(visual, dict):
        raise C1DesktopRuntimeError(
            f"{benchmark} spec lacks registered visual_oracle metadata"
        )
    spec_root = oracle_spec_path.expanduser().resolve().parent
    try:
        git_root_result = subprocess.run(
            ["git", "-C", str(spec_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise C1DesktopRuntimeError(
            f"{benchmark} registered golden git lookup failed: {error}"
        ) from error
    if git_root_result.returncode != 0:
        raise C1DesktopRuntimeError(
            f"{benchmark} registered goldens are not inside a git checkout"
        )
    git_root = Path(git_root_result.stdout.strip()).resolve()
    head_result = subprocess.run(
        ["git", "-C", str(git_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    source_commit = head_result.stdout.strip()
    if head_result.returncode != 0 or len(source_commit) != 40:
        raise C1DesktopRuntimeError(
            f"{benchmark} registered golden source commit is unavailable"
        )

    declared: dict[str, Path] = {}
    for stage in ("before", "after"):
        raw = visual.get(f"golden_{stage}")
        if not isinstance(raw, str) or not raw.strip():
            raise C1DesktopRuntimeError(
                f"{benchmark} spec lacks registered golden_{stage}"
            )
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise C1DesktopRuntimeError(
                f"{benchmark} golden_{stage} must be a contained relative path"
            )
        declared_path = spec_root / relative
        if declared_path.is_symlink():
            raise C1DesktopRuntimeError(
                f"{benchmark} golden_{stage} must not be a symlink"
            )
        source = declared_path.resolve()
        try:
            source.relative_to(spec_root)
            git_relative = source.relative_to(git_root)
        except ValueError as error:
            raise C1DesktopRuntimeError(
                f"{benchmark} golden_{stage} escapes its registered checkout"
            ) from error
        if not source.is_file():
            raise C1DesktopRuntimeError(
                f"{benchmark} registered golden_{stage} is missing: {source}"
            )
        committed = subprocess.run(
            ["git", "-C", str(git_root), "show", f"HEAD:{git_relative.as_posix()}"],
            capture_output=True,
            timeout=20,
        )
        if committed.returncode != 0:
            raise C1DesktopRuntimeError(
                f"{benchmark} registered golden_{stage} is not committed"
            )
        if committed.stdout != source.read_bytes():
            raise C1DesktopRuntimeError(
                f"{benchmark} registered golden_{stage} differs from HEAD"
            )
        declared[stage] = source
    return declared, source_commit


def _cached_registered_desktop_goldens(
    repo_commit: str | None,
    oracle_spec: dict[str, Any],
    *,
    declared: dict[str, Path],
    source_commit: str,
    benchmark: str,
    cache_root: str | Path,
) -> OracleGoldens | None:
    """Accept a desktop cache only when it is the committed registered pair."""

    cached = _cached_oracle_goldens(
        repo_commit,
        oracle_spec,
        cache_root=cache_root,
    )
    if cached is None:
        return None
    before = Path(cached.before)
    after = Path(cached.after)
    metadata_path = before.parent / "meta.json"
    try:
        metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(metadata, dict):
        return None
    consistency = metadata.get("consistency")
    expected_consistency = {
        "source": "committed_visual_oracle_pair_v1",
        "source_git_commit": source_commit,
        "golden_before": str(declared["before"]),
        "golden_after": str(declared["after"]),
    }
    if (
        metadata.get("benchmark") != benchmark
        or consistency != expected_consistency
        or _file_digest(before) != _file_digest(declared["before"])
        or _file_digest(after) != _file_digest(declared["after"])
    ):
        return None
    cached.consistency = {
        "source": "verified_cache",
        "registered_source": expected_consistency["source"],
        "source_git_commit": source_commit,
        "golden_before": str(declared["before"]),
        "golden_after": str(declared["after"]),
    }
    return cached


def _registered_desktop_state_goldens(
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    *,
    benchmark: str,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    declared, source_commit = _committed_registered_desktop_visual_pair(
        oracle_spec,
        oracle_spec_path,
        benchmark=benchmark,
    )
    repo_commit = _repo_git_commit(repo)
    cached = _cached_registered_desktop_goldens(
        repo_commit,
        oracle_spec,
        declared=declared,
        source_commit=source_commit,
        benchmark=benchmark,
        cache_root=cache_root,
    )
    if cached is not None:
        return cached
    fingerprint = _oracle_fingerprint(repo_commit, oracle_spec)
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    local_dir = report_dir / "oracle-goldens" / fingerprint
    cacheable = bool(repo_commit)
    target_dir = cache_dir if cacheable else local_dir
    with runtime_lease(target_dir):
        cached = _cached_registered_desktop_goldens(
            repo_commit,
            oracle_spec,
            declared=declared,
            source_commit=source_commit,
            benchmark=benchmark,
            cache_root=cache_root,
        )
        if cached is not None:
            return cached
        target_dir.mkdir(parents=True, exist_ok=True)
        target_before = target_dir / "golden_before.png"
        target_after = target_dir / "golden_after.png"
        shutil.copyfile(declared["before"], target_before)
        shutil.copyfile(declared["after"], target_after)
        quality = {
            "before": screenshot_quality(target_before),
            "after": screenshot_quality(target_after),
        }
        if any(
            quality[stage].get("informative") is not True
            for stage in ("before", "after")
        ):
            raise C1DesktopRuntimeError(
                f"registered {benchmark} golden pair is non-informative"
            )
        consistency = {
            "source": "committed_visual_oracle_pair_v1",
            "source_git_commit": source_commit,
            "golden_before": str(declared["before"]),
            "golden_after": str(declared["after"]),
        }
        _write_oracle_golden_metadata(
            target_dir,
            repo_commit=repo_commit,
            oracle_spec=oracle_spec,
            benchmark=benchmark,
            oracle_spec_path=oracle_spec_path,
            consistency=consistency,
            quality=quality,
            cacheable=cacheable,
        )
    return OracleGoldens(
        before=str(target_before),
        after=str(target_after),
        fingerprint=fingerprint,
        cache_hit=False,
        cacheable=cacheable,
        consistency=consistency,
        quality=quality,
        artifacts=capture_oracle_golden_artifact_digests(target_before, target_after),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def _registered_libreoffice_goldens(
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    *,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    return _registered_desktop_state_goldens(
        repo,
        oracle_spec,
        oracle_spec_path,
        report_dir,
        benchmark="LibreOffice",
        cache_root=cache_root,
    )


def _capture_c1_web_pair(
    repo: Path,
    spec: dict[str, Any],
    spec_path: Path,
    report_dir: Path,
    *,
    benchmark: str,
    headless: bool,
    oracle_spec: dict[str, Any] | None = None,
    # The live session is handed to the gate so it can be frozen for the duration
    # of the judge request, matching the GUI arm.
    start_gate: Callable[
        [str, dict[str, Any], Any], tuple[bool, dict[str, Any]]
    ]
    | None = None,
    transition_spec_factory: Callable[[], dict[str, Any] | None] | None = None,
    transition_started_callback: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Capture one semantic and visual start-to-goal pair on a live web app."""

    pair: dict[str, Any] = {
        "status": "failure",
        "failure_phase": "session_start",
        "runtime_bootstrap_ms": 0,
        "start_ms": 0,
        "goal_ms": 0,
    }
    bootstrap_started = time.perf_counter()
    try:
        session = create_c1_web_session(
            benchmark,
            repo,
            report_dir,
            headless=headless,
            require_headed=not headless,
        )
        with session:
            execution_spec = spec
            pair["runtime_bootstrap_ms"] = int(
                (time.perf_counter() - bootstrap_started) * 1000
            )
            start_started = time.perf_counter()
            try:
                restored = session.restore_target_only(spec, spec_path)
                start_grade = session.semantic_start(spec)
                pair["restore"] = restored.to_dict()
                pair["semantic_start"] = start_grade.to_dict()
                # Probe the same live session against the registered oracle
                # target so Start means the same thing here as in the GUI arms.
                oracle_grade: tuple[bool, str, dict[str, Any]] | None = None
                if isinstance(oracle_spec, dict):
                    try:
                        oracle_start = session.semantic_start(oracle_spec)
                        oracle_grade = _oracle_probe_grade(
                            oracle_start.to_dict(),
                            ok=bool(oracle_start.ok),
                            status="validate_failed",
                        )
                    except Exception as error:  # noqa: BLE001 - graded, not raised
                        oracle_grade = (
                            False,
                            "validate_error",
                            {"error": f"{type(error).__name__}: {error}"},
                        )
                merged_ok, merged_status, merged_detail = merge_start_semantic(
                    restore_ok=bool(start_grade.ok),
                    restore_status="success" if start_grade.ok else "failure",
                    restore_detail=start_grade.to_dict(),
                    oracle_grade=oracle_grade,
                )
                pair["semantic_start_merged"] = {
                    "ok": merged_ok,
                    "status": merged_status,
                    "detail": merged_detail,
                }
                if not merged_ok:
                    pair.update(
                        failure_phase="semantic_start",
                        detail=(
                            start_grade.detail
                            if not start_grade.ok
                            else "restored Start does not satisfy the registered "
                            f"oracle target ({merged_status})"
                        ),
                    )
                    return pair
                before = session.screenshot("before.png")
                pair["screenshot_before"] = before
                if not _informative_screenshot(before):
                    pair.update(
                        failure_phase="before_screenshot",
                        detail="C1 web before-state screenshot is missing or non-informative",
                    )
                    return pair
                if start_gate is not None:
                    start_ok, start_verdict = start_gate(
                        str(before), pair["semantic_start_merged"], session
                    )
                    pair["start_verdict"] = start_verdict
                    if not start_ok:
                        pair.update(
                            failure_phase="start_visual",
                            detail=(
                                "C1 web Start failed visual grading before the "
                                "registered transition"
                            ),
                        )
                        return pair
            except Exception as error:
                pair.update(
                    failure_phase="target_restore",
                    detail=f"{type(error).__name__}: {error}",
                )
                return pair
            finally:
                pair["start_ms"] = int(
                    (time.perf_counter() - start_started) * 1000
                )

            if transition_spec_factory is not None:
                planned_spec = transition_spec_factory()
                if planned_spec is None:
                    pair.update(
                        failure_phase="action_generation",
                        detail="minimal smoke action planning did not produce an executable plan",
                    )
                    return pair
                execution_spec = planned_spec

            goal_started = time.perf_counter()
            try:
                if transition_started_callback is not None:
                    transition_started_callback()
                transition = session.apply_registered_transition(execution_spec)
                pair["transition"] = transition.to_dict()
                # The transition stays the model's; the goal is graded by the
                # registered oracle so a candidate cannot weaken its own test.
                goal_spec = copy.deepcopy(execution_spec)
                if isinstance(oracle_spec, dict):
                    goal_spec["assertions"] = copy.deepcopy(
                        semantic_assertions(oracle_spec) or []
                    )
                goal_grade = session.semantic_goal(goal_spec)
                pair["semantic_goal"] = goal_grade.to_dict()
                if not goal_grade.ok:
                    pair.update(
                        failure_phase="semantic_goal",
                        detail=goal_grade.detail,
                    )
                    return pair
                after = session.screenshot("after.png")
                pair["screenshot_after"] = after
                if not _informative_screenshot(after):
                    pair.update(
                        failure_phase="after_screenshot",
                        detail="C1 web after-state screenshot is missing or non-informative",
                    )
                    return pair
            except Exception as error:
                pair.update(
                    failure_phase="registered_transition",
                    detail=f"{type(error).__name__}: {error}",
                )
                return pair
            finally:
                pair["goal_ms"] = int(
                    (time.perf_counter() - goal_started) * 1000
                )

            pair.update(
                status="success",
                failure_phase=None,
                detail=(
                    "Live target-only start, registered transition, and semantic goal "
                    "were all proven."
                ),
            )
            return pair
    except Exception as error:
        pair.update(
            failure_phase="session_start",
            detail=f"{type(error).__name__}: {error}",
            runtime_bootstrap_ms=int(
                (time.perf_counter() - bootstrap_started) * 1000
            ),
        )
        return pair


def _ensure_c1_web_goldens(
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    *,
    benchmark: str,
    headless: bool,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    """Build or reuse a digest-verified web-native semantic golden pair."""

    repo_commit = _repo_git_commit(repo)
    cached = _cached_oracle_goldens(
        repo_commit,
        oracle_spec,
        cache_root=cache_root,
    )
    cached_attestation: dict[str, Any] = {}
    if cached is not None:
        metadata_path = (
            _c1_cache_root(cache_root)
            / "state-loop-golden"
            / cached.fingerprint
            / "meta.json"
        )
        try:
            metadata = yaml.safe_load(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            metadata = None
        if isinstance(metadata, dict) and isinstance(
            metadata.get("consistency"), dict
        ):
            cached_attestation = dict(metadata["consistency"])
    if (
        cached is not None
        and cached_attestation.get("source") == "c1_web_live_semantic_pair_v1"
        and cached_attestation.get("semantic_start_ok") is True
        and cached_attestation.get("semantic_goal_ok") is True
        and cached_attestation.get("transition_actions_executed") is True
    ):
        cached.consistency = {
            "source": "verified_cache",
            "producer_source": cached_attestation.get("source"),
            "semantic_start_ok": True,
            "semantic_goal_ok": True,
            "transition_actions_executed": True,
        }
        return cached

    fingerprint = _oracle_fingerprint(repo_commit, oracle_spec)
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    local_dir = report_dir / "oracle-goldens" / fingerprint
    pair = _capture_c1_web_pair(
        repo,
        oracle_spec,
        oracle_spec_path,
        local_dir / "runtime",
        benchmark=benchmark,
        headless=headless,
        oracle_spec=oracle_spec,
    )
    if pair.get("status") != "success":
        raise RuntimeError(
            "C1 web oracle pair failed at "
            f"{pair.get('failure_phase')}: {pair.get('detail')}"
        )
    before = pair.get("screenshot_before")
    after = pair.get("screenshot_after")
    if not _informative_screenshot(before) or not _informative_screenshot(after):
        raise RuntimeError("C1 web oracle pair did not produce two informative screenshots")

    cacheable = bool(repo_commit)
    target_dir = cache_dir if cacheable else local_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_before = target_dir / "golden_before.png"
    target_after = target_dir / "golden_after.png"
    shutil.copyfile(str(before), target_before)
    shutil.copyfile(str(after), target_after)
    quality = {
        "before": screenshot_quality(target_before),
        "after": screenshot_quality(target_after),
    }
    consistency = {
        "source": "c1_web_live_semantic_pair_v1",
        "semantic_start_ok": True,
        "semantic_goal_ok": True,
        "semantic_start_source": (pair.get("semantic_start") or {}).get("source"),
        "semantic_goal_source": (pair.get("semantic_goal") or {}).get("source"),
        "transition_actions_executed": (
            (pair.get("transition") or {}).get("actions_executed") is True
        ),
    }
    if consistency["transition_actions_executed"] is not True:
        raise RuntimeError("C1 web oracle transition lacked execution attestation")
    _write_oracle_golden_metadata(
        target_dir,
        repo_commit=repo_commit,
        oracle_spec=oracle_spec,
        benchmark=benchmark,
        oracle_spec_path=oracle_spec_path,
        consistency=consistency,
        quality=quality,
        cacheable=cacheable,
    )
    return OracleGoldens(
        before=str(target_before),
        after=str(target_after),
        fingerprint=fingerprint,
        cache_hit=False,
        cacheable=cacheable,
        consistency=consistency,
        quality=quality,
        artifacts=capture_oracle_golden_artifact_digests(
            target_before,
            target_after,
        ),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def _c1_web_timing(start_ms: int, goal_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
    restore_timing = {
        "cold_restore_ms": None,
        "warm_restore_ms": max(0, int(start_ms)),
        "speedup": None,
        "fast_restore_ms": None,
        "fast_vs_full_speedup": None,
        "fast_restore_used": False,
        "fast_restore_fallback": False,
        "cache_hit": False,
    }
    after_timing = {
        **restore_timing,
        "warm_restore_ms": None,
        "fast_restore_ms": max(0, int(goal_ms)),
        "fast_restore_used": True,
    }
    return restore_timing, after_timing


def _run_c1_web_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    headless: bool,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    """Run JupyterLab Arm E without the generic browser StateSession."""

    _ensure_state_stage_metrics(result)
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(
            task.issue,
            base,
            artifact_dir=version_dir / "synthesis-artifacts",
        )
    except Exception as error:
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
        result.update(status="failed", reason=f"config synthesis failed: {error}")
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
    model_spec, reject_reason = _accepted_state_spec(
        getattr(checkpoint, "model_generated_spec", None),
        task=task,
        seed=result.get("seed"),
    )
    if model_spec is None:
        result.update(
            status="failure",
            reason=reject_reason,
            used_public_scaffold=bool(
                getattr(checkpoint, "used_public_scaffold", False)
            ),
        )
        provider_failure = _checkpoint_provider_failure(checkpoint)
        if provider_failure is not None:
            result.update(
                incomplete=True,
                reason="checkpoint synthesis provider failure: " + provider_failure,
            )
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result

    generated_spec = copy.deepcopy(model_spec)
    config_path = version_dir / "spec.yaml"
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result.update(
        config_path=str(config_path),
        used_public_scaffold=bool(
            getattr(checkpoint, "used_public_scaffold", False)
        ),
        injected_spec_source="model_generated_spec",
        state_fingerprint=state_fingerprint(
            _repo_git_commit(repo),
            generated_spec,
        ),
        generated_spec_hash=generated_spec_hash(generated_spec),
        checkpoint_synthesis_ms=synthesis_ms,
    )

    golden_started = time.perf_counter()
    try:
        goldens = _ensure_c1_web_goldens(
            repo,
            oracle_spec,
            oracle_spec_path,
            task_dir / "oracle",
            benchmark=task.benchmark,
            headless=headless,
        )
        result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
    except Exception as error:
        result.update(
            status="failed",
            reason=f"oracle golden generation failed: {error}",
            grader_ms=int((time.perf_counter() - golden_started) * 1000),
        )
        return result
    result["grader_ms"] = int((time.perf_counter() - golden_started) * 1000)

    def _start_gate(
        live_before: str, merged_start: dict[str, Any], runtime: Any
    ) -> tuple[bool, dict[str, Any]]:
        _mark_state_start_semantic(
            result,
            ok=merged_start.get("ok") is True,
            status=str(merged_start.get("status") or "failure"),
            detail=merged_start.get("detail"),
        )
        if result.get("before_semantic_ok") is not True:
            _freeze_pair_start_semantic_failure(
                result,
                task=task,
                accepted_spec=generated_spec,
                synthesis_agent=agent,
                live_before=live_before,
            )
            return False, copy.deepcopy(
                (result.get("pair_start_gate") or {}).get("pair_verdict") or {}
            )
        return _run_state_start_visual_gate(
            result,
            live_before=live_before,
            goldens=goldens,
            task=task,
            runtime=runtime,
            accepted_spec=generated_spec,
            synthesis_agent=agent,
        )

    def _transition_spec_factory() -> dict[str, Any] | None:
        return _plan_minimal_smoke_transition(
            result,
            planner=minimal_planner,
            base=base,
            task=task,
            generated_spec=generated_spec,
            task_dir=version_dir,
            seed=int(result.get("seed", 0)),
        )

    pair = _capture_c1_web_pair(
        repo,
        generated_spec,
        config_path,
        task_dir / "candidate",
        benchmark=task.benchmark,
        headless=headless,
        oracle_spec=oracle_spec,
        start_gate=_start_gate,
        transition_spec_factory=(
            _transition_spec_factory
            if transition_action_source in PLANNED_TRANSITION_ACTION_SOURCES
            else None
        ),
        transition_started_callback=lambda: _mark_transition_action_execution_started(
            result
        ),
    )
    start_ms = int(pair.get("start_ms", 0) or 0)
    goal_ms = int(pair.get("goal_ms", 0) or 0)
    restore_timing, after_timing = _c1_web_timing(start_ms, goal_ms)
    start_grade = pair.get("semantic_start")
    goal_grade = pair.get("semantic_goal")
    before_shot = pair.get("screenshot_before")
    after_shot = pair.get("screenshot_after")
    result.update(
        restore_timing=restore_timing,
        after_timing=after_timing,
        screenshot_before=before_shot,
        screenshot_after=after_shot,
        initial_state_fingerprint=_image_fingerprint(before_shot),
        semantic_start=start_grade,
        semantic_goal=goal_grade,
        registered_transition=pair.get("transition"),
        runtime_report=pair,
        session_bootstrap_ms=int(pair.get("runtime_bootstrap_ms", 0) or 0),
        runtime_bootstrap_ms=int(pair.get("runtime_bootstrap_ms", 0) or 0),
        restore_status=(
            "success" if isinstance(start_grade, dict) and start_grade.get("ok") is True else "failure"
        ),
        after_status=(
            "success" if isinstance(goal_grade, dict) and goal_grade.get("ok") is True else "failure"
        ),
    )
    semantic_start_observed = bool(
        isinstance(start_grade, dict) and start_grade.get("ok") is True
    )
    merged_start = pair.get("semantic_start_merged")
    if not isinstance(merged_start, dict):
        merged_start = {
            "ok": False,
            "status": ORACLE_TARGET_UNSUPPORTED,
            "detail": start_grade,
        }
    if (
        isinstance(start_grade, dict)
        and result.get("before_semantic_status") == "not_evaluated"
    ):
        _mark_state_start_semantic(
            result,
            ok=merged_start.get("ok") is True,
            status=str(merged_start.get("status") or "failure"),
            detail=merged_start.get("detail"),
        )
    result["seed_evidence"] = {
        "schema_version": 1,
        "source": "c1_web_live_session",
        "observed": semantic_start_observed,
        "semantic_observed": semantic_start_observed,
        "initial_semantic_source": (
            start_grade.get("source") if isinstance(start_grade, dict) else None
        ),
        "initial_semantic_stage": (
            "restored" if isinstance(start_grade, dict) else "unobserved"
        ),
        "initial_semantic_fingerprint": (
            start_grade.get("fingerprint")
            if isinstance(start_grade, dict)
            else None
        ),
        "initial_semantic_observation": (
            start_grade.get("observation")
            if isinstance(start_grade, dict)
            else None
        ),
    }

    if pair.get("status") != "success":
        failure_phase = str(pair.get("failure_phase") or "runtime")
        if failure_phase == "before_screenshot" and semantic_start_observed:
            _mark_state_start_visual_incomplete(result)
        elif failure_phase in {
            "registered_transition",
            "action_generation",
            "semantic_goal",
            "after_screenshot",
        }:
            semantic_goal_ok = bool(
                isinstance(goal_grade, dict) and goal_grade.get("ok") is True
            )
            _mark_state_goal_semantic(
                result,
                ok=semantic_goal_ok,
                status=(
                    "success"
                    if semantic_goal_ok
                    else "transition_failure"
                    if failure_phase == "registered_transition"
                    else "failure"
                ),
                detail=goal_grade or pair.get("detail"),
            )
            if failure_phase == "after_screenshot" and semantic_goal_ok:
                _mark_state_goal_visual_incomplete(result)
        result.update(
            status="failure",
            reason=(
                f"C1 web pair failed at {failure_phase}: "
                f"{pair.get('detail') or 'unknown failure'}"
            ),
            iterations=[
                {
                    "iteration": 1,
                    "result": False,
                    "status": (
                        "restore_failure"
                        if failure_phase
                        in {
                            "session_start",
                            "target_restore",
                            "semantic_start",
                            "before_screenshot",
                        }
                        else "smoke_failure"
                    ),
                    "restore_timing": restore_timing,
                    "after_timing": after_timing,
                    "screenshot_before": before_shot,
                    "screenshot_after": after_shot,
                    "semantic_start": start_grade,
                    "semantic_goal": goal_grade,
                    "failure_phase": failure_phase,
                }
            ],
        )
        return result

    _mark_state_goal_semantic(
        result,
        ok=isinstance(goal_grade, dict) and goal_grade.get("ok") is True,
        status=(
            "success"
            if isinstance(goal_grade, dict) and goal_grade.get("ok") is True
            else "failure"
        ),
        detail=goal_grade,
    )
    verdict = _run_state_goal_visual_grade(
        result,
        before_verdict=(
            pair.get("start_verdict")
            if isinstance(pair.get("start_verdict"), dict)
            else result.get("pair_verdict") or {}
        ),
        live_before=str(before_shot),
        live_after=str(after_shot),
        goldens=goldens,
        task=task,
    )
    passed = result.get("e2e_success") is True
    result.update(
        pair_verdict=verdict,
        result=passed,
        status="success" if passed else "failure",
        iterations=[
            {
                "iteration": 1,
                "result": passed,
                "status": "success" if passed else "failure",
                "restore_timing": restore_timing,
                "after_timing": after_timing,
                "screenshot_before": before_shot,
                "screenshot_after": after_shot,
                "semantic_start": start_grade,
                "semantic_goal": goal_grade,
                "registered_transition": pair.get("transition"),
                "pair_verdict": verdict,
            }
        ],
    )
    return result


def _unity_checkpoint_adapter(
    benchmark: str,
) -> type[JynewRuntimeAdapter] | type[Pal3RuntimeAdapter]:
    normalized = str(benchmark).strip().lower()
    if normalized == "jynew":
        return JynewRuntimeAdapter
    if normalized in {"pal3", "pal3.unity"}:
        return Pal3RuntimeAdapter
    raise ValueError(f"unsupported Unity C1 benchmark: {benchmark!r}")


def _pal3_oracle_start_assertions(
    target: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Mirror the PAL3 ``CheckC1Target`` predicates as runner assertions.

    The state arm drives Unity through the one-shot batch runner, which offers
    no live semantic probe.  Expressing the oracle Start target in the runner's
    own assertion vocabulary lets the same before-pass grade the restored state
    against the oracle without ever restoring the oracle target.  ``None`` means
    a field had no faithful equivalent and the Start must fail closed.
    """

    assertions: list[dict[str, Any]] = []
    for key, value in target.items():
        # Synthesis directives, not state predicates: PAL3's own CheckC1Target
        # ignores them too, so they contribute no assertion.
        if key in {
            "kind",
            "save_index",
            "validations",
            "make_playable",
            "player_tile_x",
            "player_tile_y",
            "player_nav_layer",
        }:
            continue
        if key in {"scene_city", "scene_name"}:
            continue
        if key == "money":
            if int(value) > 0:
                assertions.append({"type": "pal3_money_gte", "int_value": int(value)})
            continue
        if key == "team_ids":
            assertions.extend(
                {"type": "pal3_team_contains", "actor_id": int(actor)}
                for actor in value or []
            )
            continue
        if key == "story_vars":
            assertions.extend(
                {
                    "type": "pal3_story_var_equals",
                    "var_id": int(entry["key"]),
                    "expected": str(entry["value"]),
                    "comparator": "equals",
                }
                for entry in value or []
            )
            continue
        if key == "items":
            assertions.extend(
                {
                    "type": "pal3_item_count",
                    "item_id": int(entry["id"]),
                    "expected": str(entry["count"]),
                    "comparator": "gte",
                }
                for entry in value or []
            )
            continue
        if key == "world_regions":
            assertions.extend(
                {
                    "type": "pal3_world_region_enabled",
                    "region": int(entry["region"]),
                    "int_value": int(entry["flag"]),
                }
                for entry in value or []
            )
            continue
        if key == "favors":
            assertions.extend(
                {
                    "type": "pal3_favor_gte",
                    "actor_id": int(entry["actor_id"]),
                    "int_value": int(entry["amount"]),
                }
                for entry in value or []
            )
            continue
        return None
    scene_city = target.get("scene_city")
    scene_name = target.get("scene_name")
    if scene_city or scene_name:
        assertions.append(
            {"type": "pal3_scene_is", "city": scene_city, "scene": scene_name}
        )
    return assertions


def _jynew_oracle_start_assertions(
    target: dict[str, Any]
) -> list[dict[str, Any]] | None:
    """Mirror the jynew ``CheckC1Target`` predicates as runner assertions."""

    assertions: list[dict[str, Any]] = []
    for key, value in target.items():
        if key in {"kind", "mod_id", "validations"}:
            continue
        if key == "player_level":
            if int(value) > 0:
                assertions.append(
                    {
                        "type": "jynew_role_attr",
                        "role_id": 0,
                        "attr": "Level",
                        "expected": str(int(value)),
                        "comparator": "equals",
                    }
                )
            continue
        if key == "money":
            if int(value) > 0:
                assertions.append({"type": "jynew_money_gte", "int_value": int(value)})
            continue
        if key == "map_id":
            if int(value) >= 0:
                assertions.append({"type": "jynew_map_id", "int_value": int(value)})
            continue
        if key == "team_ids":
            # The runner treats role 0 as always present in the team check, so
            # asserting it would be stricter than the target predicate.
            assertions.extend(
                {"type": "jynew_team_contains", "role_id": int(role)}
                for role in value or []
                if int(role) != 0
            )
            continue
        if key == "items":
            assertions.extend(
                {
                    "type": "jynew_item_count",
                    "item_id": int(entry["id"]),
                    "expected": str(entry["count"]),
                    "comparator": "gte",
                }
                for entry in value or []
            )
            continue
        if key == "key_values":
            assertions.extend(
                {
                    "type": "jynew_key_value_equals",
                    "key": entry["key"],
                    "str_value": entry["value"],
                }
                for entry in value or []
            )
            continue
        # ``skills`` grades a level threshold that jynew_skill_learned cannot
        # express, so any remaining field fails closed rather than approximate.
        return None
    return assertions


def _unity_oracle_start_assertions(
    benchmark: str, oracle_spec: dict[str, Any] | None
) -> list[dict[str, Any]] | None:
    if not isinstance(oracle_spec, dict):
        return None
    target = oracle_target(oracle_spec)
    if not target:
        return None
    normalized = str(benchmark).strip().lower()
    try:
        if normalized == "jynew":
            return _jynew_oracle_start_assertions(target)
        if normalized in {"pal3", "pal3.unity"}:
            return _pal3_oracle_start_assertions(target)
    except (KeyError, TypeError, ValueError):
        return None
    return None


def _mark_unity_goal_semantic(
    result: dict[str, Any], pair: dict[str, Any]
) -> bool:
    """Grade the Unity goal from the after pass's oracle-assertion checks.

    The batch runner folds assertion results into its aggregate report status, so
    reading that status alone would look correct while leaving the code blind to
    whether the goal was actually reached. Read the ``assertion[...]`` checks
    directly instead, the same way Start reads them.
    """

    after = pair.get("after")
    after = after if isinstance(after, dict) else {}
    runtime_checks, oracle_checks = _unity_check_split(after)
    runtime_ok = after.get("status") == "success" and all(
        check.get("status") == "success" for check in runtime_checks
    )
    oracle_ok = bool(oracle_checks) and all(
        check.get("status") == "success" for check in oracle_checks
    )
    ok = bool(runtime_ok and oracle_ok)
    if not runtime_ok:
        status = str(after.get("status") or "failure")
    elif not oracle_checks:
        status = ORACLE_TARGET_UNSUPPORTED
    else:
        status = "success" if ok else "assert_failed"
    _mark_state_goal_semantic(result, ok=ok, status=status, detail=after)
    return ok


def _unity_check_split(
    pass_result: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split a Unity pass report into runtime checks and oracle-assertion checks."""

    checks = pass_result.get("checks")
    checks = checks if isinstance(checks, list) else []
    oracle = [
        check
        for check in checks
        if isinstance(check, dict)
        and str(check.get("name") or "").startswith("assertion[")
    ]
    restore = [check for check in checks if check not in oracle]
    return restore, oracle


def _run_unity_checkpoint_pair(
    repo: Path,
    spec: dict[str, Any],
    spec_path: Path,
    report_dir: Path,
    *,
    benchmark: str,
    headless: bool,
    task_id: str | None = None,
    experiment_seed: int = 0,
    oracle_start_assertions: list[dict[str, Any]] | None = None,
    oracle_goal_assertions: list[dict[str, Any]] | None = None,
    start_gate: Callable[
        [str | None, dict[str, Any]], tuple[bool, dict[str, Any]]
    ]
    | None = None,
    transition_spec_factory: Callable[[], dict[str, Any] | None] | None = None,
    transition_started_callback: Callable[[], None] | None = None,
) -> tuple[dict[str, Any], str | None, str | None, int, int]:
    """Run a target-only and a full Unity utility pass with real captures."""

    adapter_type = _unity_checkpoint_adapter(benchmark)
    start_spec = copy.deepcopy(spec)
    start_spec["actions"] = []
    # The before pass restores the arm's OWN target and then evaluates the
    # oracle Start target as assertions, so Start means the same thing here as
    # in the GUI arms' live ``semantic_start`` probe.
    start_spec["assertions"] = list(oracle_start_assertions or [])
    before_started = time.perf_counter()
    with adapter_type(
        repo, report_dir / "before", headless=headless, visual_capture=True
    ) as runtime:
        before_result = runtime.run_checkpoint(
            start_spec,
            spec_path,
            **(
                {
                    "evidence_task_id": task_id,
                    "evidence_seed": int(experiment_seed),
                    "evidence_stage": "before",
                    "evidence_benchmark": _canonical_state_evidence_benchmark(
                        benchmark
                    ),
                }
                if task_id is not None
                else {}
            ),
        )
        before_shot = (
            runtime.screenshot("unity-before.png")
            if before_result.get("status") == "success"
            else None
        )
    before_ms = int((time.perf_counter() - before_started) * 1000)

    if start_gate is not None:
        start_ok, start_verdict = start_gate(before_shot, before_result)
        if not start_ok:
            return (
                {
                    "status": "start_gate_failure",
                    "failure_phase": (
                        "start_visual"
                        if before_result.get("status") == "success"
                        and before_shot is not None
                        else "semantic_start"
                    ),
                    "before": before_result,
                    "after": None,
                    "start_verdict": start_verdict,
                },
                before_shot,
                None,
                before_ms,
                0,
            )

    goal_spec = copy.deepcopy(spec)
    if transition_spec_factory is not None:
        planned_spec = transition_spec_factory()
        if planned_spec is None:
            return (
                {
                    "status": "action_generation_failure",
                    "failure_phase": "action_generation",
                    "before": before_result,
                    "after": None,
                },
                before_shot,
                None,
                before_ms,
                0,
            )
        goal_spec = planned_spec
    # The after pass keeps the candidate's transition but is graded by the
    # registered oracle's assertions.
    if oracle_goal_assertions is not None:
        goal_spec["assertions"] = list(oracle_goal_assertions)

    after_started = time.perf_counter()
    with adapter_type(
        repo, report_dir / "after", headless=headless, visual_capture=True
    ) as runtime:
        if transition_started_callback is not None:
            transition_started_callback()
        after_result = runtime.run_checkpoint(
            goal_spec,
            spec_path,
            **(
                {
                    "evidence_task_id": task_id,
                    "evidence_seed": int(experiment_seed),
                    "evidence_stage": "after",
                    "evidence_benchmark": _canonical_state_evidence_benchmark(
                        benchmark
                    ),
                }
                if task_id is not None
                else {}
            ),
        )
        after_shot = (
            runtime.screenshot("unity-after.png")
            if after_result.get("status") == "success"
            else None
        )
    after_ms = int((time.perf_counter() - after_started) * 1000)
    combined = {
        "status": (
            "success"
            if before_result.get("status") == "success"
            and after_result.get("status") == "success"
            else "failure"
        ),
        "before": before_result,
        "after": after_result,
    }
    return combined, before_shot, after_shot, before_ms, after_ms


def _ensure_unity_checkpoint_goldens(
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    *,
    benchmark: str,
    headless: bool,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    repo_commit = _repo_git_commit(repo)
    cached = _cached_oracle_goldens(repo_commit, oracle_spec, cache_root=cache_root)
    if cached is not None:
        return cached
    fingerprint = _oracle_fingerprint(repo_commit, oracle_spec)
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    local_dir = report_dir / "oracle-goldens" / fingerprint
    pair, before, after, _, _ = _run_unity_checkpoint_pair(
        repo,
        oracle_spec,
        oracle_spec_path,
        local_dir / "runtime",
        benchmark=benchmark,
        headless=headless,
    )
    if pair.get("status") != "success":
        error_type = (
            UnityOracleInfrastructureError
            if _unity_oracle_pair_infrastructure_failure(pair)
            else RuntimeError
        )
        raise error_type(f"Unity oracle spec failed: {pair!r}")
    if not _informative_screenshot(before):
        raise UnityOracleInfrastructureError(
            f"Unity oracle before-state screenshot is missing or non-informative: {before}"
        )
    if not _informative_screenshot(after):
        raise UnityOracleInfrastructureError(
            f"Unity oracle after-state screenshot is missing or non-informative: {after}"
        )

    cacheable = bool(repo_commit)
    target_dir = cache_dir if cacheable else local_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    target_before = target_dir / "golden_before.png"
    target_after = target_dir / "golden_after.png"
    shutil.copyfile(str(before), target_before)
    shutil.copyfile(str(after), target_after)
    quality = {
        "before": screenshot_quality(target_before),
        "after": screenshot_quality(target_after),
    }
    consistency = {"source": "unity_target_only_and_action_capture"}
    _write_oracle_golden_metadata(
        target_dir,
        repo_commit=repo_commit,
        oracle_spec=oracle_spec,
        benchmark=benchmark,
        oracle_spec_path=oracle_spec_path,
        consistency=consistency,
        quality=quality,
        cacheable=cacheable,
    )
    return OracleGoldens(
        before=str(target_before),
        after=str(target_after),
        fingerprint=fingerprint,
        cache_hit=False,
        cacheable=cacheable,
        consistency=consistency,
        quality=quality,
        artifacts=capture_oracle_golden_artifact_digests(
            target_before, target_after
        ),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def _run_unity_checkpoint_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    headless: bool,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    _ensure_state_stage_metrics(result)
    _record_state_runtime_not_started(result)
    result["injected_spec_source"] = "none"
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(
            task.issue,
            base,
            artifact_dir=version_dir / "synthesis-artifacts",
        )
    except Exception as error:
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
        result.update(status="failed", reason=f"config synthesis failed: {error}")
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
    result.update(
        checkpoint_synthesis_ms=synthesis_ms,
        used_public_scaffold=bool(
            getattr(checkpoint, "used_public_scaffold", False)
        ),
    )
    model_spec = getattr(checkpoint, "model_generated_spec", None)
    config_path = version_dir / "spec.yaml"
    compatibility_errors = _unity_state_model_spec_errors(
        model_spec,
        task=task,
        oracle_spec=oracle_spec,
        spec_path=config_path,
    )
    if compatibility_errors:
        provider_failure = _checkpoint_provider_failure(checkpoint)
        result.update(
            status="failure",
            generated_spec_contract_errors=compatibility_errors,
            reason=(
                "Unity synthesis produced no usable task-compatible model spec; "
                "the State arm does not fall back to the public scaffold"
            ),
        )
        if provider_failure is not None:
            result.update(
                status="incomplete",
                incomplete=True,
                reason="checkpoint synthesis provider failure: " + provider_failure,
            )
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    try:
        generated_spec = _apply_experiment_seed(
            copy.deepcopy(model_spec),
            result.get("seed"),
            benchmark=task.benchmark,
        )
    except Exception as error:
        result.update(
            status="failure",
            generated_spec_contract_errors=[str(error)],
            reason="Unity model-generated spec could not be seeded",
        )
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result.update(
        config_path=str(config_path),
        injected_spec_source="model_generated_spec",
        state_fingerprint=state_fingerprint(
            _repo_git_commit(repo), generated_spec
        ),
        generated_spec_hash=generated_spec_hash(generated_spec),
    )

    try:
        goldens = _ensure_unity_checkpoint_goldens(
            repo,
            oracle_spec,
            oracle_spec_path,
            task_dir / "session",
            benchmark=task.benchmark,
            headless=headless,
        )
        result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
    except Exception as error:
        result.update(
            status="failed",
            incomplete=True,
            infrastructure_error=(
                getattr(error, "infrastructure_error", None) is True
            ),
            reason=f"oracle golden generation failed: {error}",
        )
        return result

    _record_state_runtime_attempted(result)

    oracle_start_assertions = _unity_oracle_start_assertions(
        task.benchmark, oracle_spec
    )

    def _start_gate(
        live_before: str | None, before_result: dict[str, Any]
    ) -> tuple[bool, dict[str, Any]]:
        restore_checks, oracle_checks = _unity_check_split(before_result)
        restore_ok = all(
            check.get("status") == "success" for check in restore_checks
        )
        oracle_grade: tuple[bool, str, dict[str, Any]] | None = None
        if oracle_start_assertions:
            oracle_grade = _oracle_probe_grade(
                {"checks": oracle_checks},
                ok=bool(oracle_checks)
                and all(
                    check.get("status") == "success" for check in oracle_checks
                ),
            )
        semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
            restore_ok=restore_ok,
            restore_status="success" if restore_ok else "failure",
            restore_detail={"checks": restore_checks},
            oracle_grade=oracle_grade,
        )
        _mark_state_start_semantic(
            result,
            ok=semantic_ok,
            status=semantic_status,
            detail=semantic_detail,
        )
        if not semantic_ok:
            _freeze_pair_start_semantic_failure(
                result,
                task=task,
                accepted_spec=generated_spec,
                synthesis_agent=agent,
                live_before=str(live_before) if live_before else None,
            )
            return False, {}
        if not _informative_screenshot(live_before):
            _mark_state_start_visual_incomplete(result)
            return False, {}
        # The Unity adapter is already torn down when the judge runs, so the
        # graded start cannot drift and there is nothing left to suspend.
        return _run_state_start_visual_gate(
            result,
            live_before=str(live_before),
            goldens=goldens,
            task=task,
            no_runtime_backend=TERMINATED_BACKEND,
            accepted_spec=generated_spec,
            synthesis_agent=agent,
        )

    def _transition_spec_factory() -> dict[str, Any] | None:
        return _plan_minimal_smoke_transition(
            result,
            planner=minimal_planner,
            base=base,
            task=task,
            generated_spec=generated_spec,
            task_dir=version_dir,
            seed=int(result.get("seed", 0)),
        )

    try:
        pair, before_shot, after_shot, before_ms, after_ms = (
            _run_unity_checkpoint_pair(
                repo,
                generated_spec,
                config_path,
                task_dir / "candidate",
                benchmark=task.benchmark,
                headless=headless,
                task_id=task.id,
                experiment_seed=int(result.get("seed", 0) or 0),
                oracle_start_assertions=oracle_start_assertions,
                oracle_goal_assertions=copy.deepcopy(
                    semantic_assertions(oracle_spec) or []
                ),
                start_gate=_start_gate,
                transition_spec_factory=(
                    _transition_spec_factory
                    if transition_action_source
                    in PLANNED_TRANSITION_ACTION_SOURCES
                    else None
                ),
                transition_started_callback=lambda: _mark_transition_action_execution_started(
                    result
                ),
            )
        )
    except Exception as error:
        result.update(status="failed", reason=f"Unity checkpoint runtime failed: {error}")
        return result

    evidence_seed = int(result.get("seed", 0) or 0)
    before_seed_snapshot = _unity_state_seed_snapshot(
        pair.get("before") if isinstance(pair.get("before"), dict) else {},
        benchmark=task.benchmark,
        task_id=task.id,
        seed=evidence_seed,
        stage="before",
    )
    after_seed_snapshot = None
    if isinstance(pair.get("after"), dict):
        after_seed_snapshot = _unity_state_seed_snapshot(
            pair["after"],
            benchmark=task.benchmark,
            task_id=task.id,
            seed=evidence_seed,
            stage="after",
        )
    if pair.get("status") == "start_gate_failure":
        _record_state_process_start_evidence(
            result,
            source=UNITY_STATE_PAIR_SOURCE,
            algorithm=UNITY_STATE_PAIR_ALGORITHM,
            initial=before_seed_snapshot,
        )
    else:
        _record_state_process_pair_evidence(
            result,
            source=UNITY_STATE_PAIR_SOURCE,
            algorithm=UNITY_STATE_PAIR_ALGORITHM,
            initial=before_seed_snapshot,
            final=after_seed_snapshot or {},
        )

    restore_timing = {
        "cold_restore_ms": None,
        "warm_restore_ms": before_ms,
        "speedup": None,
        "fast_restore_ms": None,
        "fast_vs_full_speedup": None,
        "fast_restore_used": False,
        "fast_restore_fallback": False,
        "cache_hit": False,
    }
    after_timing = {
        **restore_timing,
        "warm_restore_ms": None,
        "fast_restore_ms": after_ms,
        "fast_restore_used": True,
    }
    result.update(
        restore_timing=restore_timing,
        after_timing=after_timing,
        screenshot_before=before_shot,
        screenshot_after=after_shot,
        runtime_report=pair,
    )
    if pair.get("status") != "success":
        if pair.get("status") != "start_gate_failure":
            _mark_unity_goal_semantic(result, pair)
        result.update(
            status="failure",
            reason=(
                "Unity checkpoint Start gate failed"
                if pair.get("status") == "start_gate_failure"
                else "Unity checkpoint pair failed"
            ),
        )
        result["iterations"] = [
            {
                "iteration": 1,
                "result": False,
                "status": "runtime_failure",
                "restore_timing": restore_timing,
            }
        ]
        return result
    if not _informative_screenshot(before_shot) or not _informative_screenshot(
        after_shot
    ):
        result.update(
            status="failure",
            reason="Unity checkpoint pair produced a missing or non-informative capture",
        )
        _mark_unity_goal_semantic(result, pair)
        _mark_state_goal_visual_incomplete(result)
        return result

    if not _mark_unity_goal_semantic(result, pair):
        result.update(
            status="failure",
            reason="Unity goal did not satisfy the registered oracle assertions",
        )
        return result
    verdict = _run_state_goal_visual_grade(
        result,
        before_verdict=(
            pair.get("start_verdict")
            if isinstance(pair.get("start_verdict"), dict)
            else result.get("pair_verdict") or {}
        ),
        live_before=str(before_shot),
        live_after=str(after_shot),
        goldens=goldens,
        task=task,
    )
    passed = result.get("e2e_success") is True
    result.update(
        pair_verdict=verdict,
        result=passed,
        status="success" if passed else "failure",
        iterations=[
            {
                "iteration": 1,
                "result": passed,
                "status": "success" if passed else "failure",
                "restore_timing": restore_timing,
                "screenshot_before": before_shot,
                "screenshot_after": after_shot,
                "pair_verdict": verdict,
            }
        ],
    )
    return result


def _informative_screenshot(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    path = Path(value)
    if not path.exists():
        return False
    return bool(screenshot_quality(path)["informative"])


def _repo_git_commit(repo: Path) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return completed.stdout.strip()
    except Exception:
        return None


def _oracle_repo_identity(repo: Path, oracle_spec: dict[str, Any]) -> str | None:
    """Bind Luanti caches to the exact instrumented client, not only Git HEAD."""

    if str(oracle_spec.get("benchmark") or "").strip().lower() == "luanti":
        try:
            return luanti_client_source_identity(repo)
        except Exception:
            return None
    return _repo_git_commit(repo)


def _ensure_luanti_goldens(
    session: LuantiWarmSession,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    report_dir: Path,
    cache_root: str | Path = ".warptest-cache",
) -> OracleGoldens:
    """Generate before/after oracle goldens on the warm luanti session (Repo_Oracle)."""
    repo_commit = getattr(session, "runtime_identity", None)
    if not isinstance(repo_commit, str) or not repo_commit.strip():
        repo_commit = session.git_commit if hasattr(session, "git_commit") else None
    cached = _cached_oracle_goldens(repo_commit, oracle_spec, cache_root=cache_root)
    if cached is not None:
        return cached
    fingerprint = _oracle_fingerprint(repo_commit, oracle_spec)
    after_description, after_rubric = _oracle_visual_guidance(oracle_spec)
    cache_dir = _c1_cache_root(cache_root) / "state-loop-golden" / fingerprint
    actions = oracle_spec.get("actions", [])
    before = session.restore(oracle_spec, actions=[], assertions=[])
    before_shot = session.screenshot("oracle-before.png")
    if before.status != "success" or not before_shot or not Path(before_shot).exists():
        raise RuntimeError(f"oracle before-state capture failed: {before.detail}")
    after = session.restore(oracle_spec, actions=actions)
    after_shot = session.screenshot("oracle-after.png")
    if after.status != "success" or not after_shot or not Path(after_shot).exists():
        raise RuntimeError(f"oracle after-state screenshot capture failed: {after.detail}")
    before_q = screenshot_quality(Path(before_shot))
    after_q = screenshot_quality(Path(after_shot))
    if not before_q.get("informative"):
        raise RuntimeError(f"oracle before-state screenshot is non-informative: {before_q}")
    if not after_q.get("informative"):
        raise RuntimeError(f"oracle after-state screenshot is non-informative: {after_q}")
    cacheable = bool(repo_commit)
    target_dir = (
        cache_dir
        if cacheable
        else report_dir / "oracle-goldens" / fingerprint
    )
    target_dir.mkdir(parents=True, exist_ok=True)
    golden_before = target_dir / "golden_before.png"
    golden_after = target_dir / "golden_after.png"
    shutil.copyfile(before_shot, golden_before)
    shutil.copyfile(after_shot, golden_after)
    consistency = {
        "source": "luanti_warm",
        "runtime_identity": repo_commit,
    }
    quality = {"before": before_q, "after": after_q}
    _write_oracle_golden_metadata(
        target_dir,
        repo_commit=repo_commit,
        oracle_spec=oracle_spec,
        benchmark="luanti",
        oracle_spec_path=oracle_spec_path,
        consistency=consistency,
        quality=quality,
        cacheable=cacheable,
    )
    return OracleGoldens(
        before=str(golden_before),
        after=str(golden_after),
        fingerprint=fingerprint,
        cache_hit=False,
        cacheable=cacheable,
        consistency=consistency,
        quality=quality,
        artifacts=capture_oracle_golden_artifact_digests(golden_before, golden_after),
        after_description=after_description,
        after_rubric=after_rubric,
    )


def _run_luanti_task(
    base: Path,
    task: StateLoopTask,
    agent: Any,
    repo: Path,
    oracle_spec: dict[str, Any],
    oracle_spec_path: Path,
    result: dict[str, Any],
    *,
    task_dir: Path,
    seed: int = 0,
    restore_diagnostics: bool = True,
    minimal_planner: MinimalSmokeActionAgent | None = None,
    transition_action_source: str = "generated_spec",
) -> dict[str, Any]:
    """Run one luanti state-loop task on a persistent rendered warm session.

    Mirrors the browser path's result shape: synthesize a config, generate oracle
    goldens, capture candidate before/after frames (full synth + skip-synthesis
    fast-restore), grade the pair, and report skip-spawn (cold/warm) + skip-synthesis
    (fast-vs-full) timing.
    """

    _ensure_state_stage_metrics(result)
    issue = task.issue
    version_dir = task_dir / "v1"
    version_dir.mkdir(parents=True, exist_ok=True)
    synthesis_started = time.perf_counter()
    try:
        checkpoint = agent.synthesize(
            issue,
            base,
            artifact_dir=version_dir / "synthesis-artifacts",
        )
    except Exception as error:
        synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
        result["status"] = "failed"
        result["reason"] = f"config synthesis failed: {error}"
        result["iterations"] = [
            {
                "iteration": 1,
                "result": False,
                "status": "synthesis_failure",
                "reason": str(error),
            }
        ]
        _set_state_method_timing(result, checkpoint_synthesis_ms=synthesis_ms)
        return result
    synthesis_ms = int((time.perf_counter() - synthesis_started) * 1000)
    result["checkpoint_synthesis_ms"] = synthesis_ms
    result["used_public_scaffold"] = bool(
        getattr(checkpoint, "used_public_scaffold", False)
    )
    generated_spec, reject_reason = _accepted_state_spec(
        getattr(checkpoint, "model_generated_spec", None), task=task, seed=seed
    )
    if generated_spec is None:
        provider_failure = _checkpoint_provider_failure(checkpoint)
        if provider_failure is not None:
            reason = "checkpoint synthesis provider failure: " + provider_failure
            result["incomplete"] = True
            result["status"] = "incomplete"
        else:
            reason = reject_reason
            result["status"] = "failure"
        result["injected_spec_source"] = "none"
        result["reason"] = reason
        result["iterations"] = [
            {
                "iteration": 1,
                "result": False,
                "status": "synthesis_failure",
                "used_public_scaffold": result["used_public_scaffold"],
                "injected_spec_source": "none",
                "reason": reason,
                "incomplete": provider_failure is not None,
            }
        ]
        _set_state_method_timing(result)
        return result

    config_path = version_dir / "spec.yaml"
    config_path.write_text(
        yaml.safe_dump(generated_spec, sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )
    result["injected_spec_source"] = "model_generated_spec"
    result["generated_spec_hash"] = generated_spec_hash(generated_spec)
    daylight_mode = luanti_daylight_mode_for_task(task, oracle_spec)
    result["luanti_daylight_mode"] = daylight_mode

    # Cold spawn and the fast-vs-full probe are optional diagnostics, not part of
    # E's measured method path. Formal C1 keeps them off unless the caller selects
    # this task for a separate diagnostic sample.
    cold_ms = (
        _measure_luanti_cold(
            repo,
            generated_spec,
            task_dir / "cold-baseline",
            seed=seed,
        )
        if restore_diagnostics
        else None
    )
    if restore_diagnostics:
        result["cold_restore_ms"] = cold_ms

    goldens = lookup_cached_oracle_goldens(repo, oracle_spec)

    try:
        session = LuantiWarmSession(
            repo,
            task_dir / "session",
            render=True,
            experiment_seed=seed,
            daylight_mode=daylight_mode,
        ).start()
    except Exception as error:
        result["status"] = "failed"
        result["reason"] = f"luanti warm session bootstrap failed: {error}"
        _set_state_method_timing(result)
        return result
    try:
        result["session_bootstrap_ms"] = session.bootstrap_ms
        golden_started = time.perf_counter()
        try:
            if goldens is None:
                goldens = _ensure_luanti_goldens(
                    session,
                    oracle_spec,
                    oracle_spec_path,
                    task_dir / "session",
                )
            result["oracle_goldens"] = oracle_goldens_report_payload(goldens)
        except Exception as error:
            result["status"] = "failed"
            result["reason"] = f"oracle golden generation failed: {error}"
            result["grader_ms"] = int(
                (time.perf_counter() - golden_started) * 1000
            )
            _set_state_method_timing(result)
            return result
        result["grader_ms"] = int((time.perf_counter() - golden_started) * 1000)

        # Candidate before: full synth (no smoke) + screenshot.
        before = session.restore(generated_spec, actions=[], assertions=[])
        before_shot = session.screenshot("before-v1.png")
        # skip_synthesis validation only: read the live world and check it
        # against the oracle Start target without mutating anything.
        start_oracle_grade: tuple[bool, str, dict[str, Any]] | None = None
        if before.status == "success" and isinstance(oracle_spec, dict):
            try:
                oracle_outcome = session.fast_restore(
                    oracle_spec, actions=[], assertions=[]
                )
                start_oracle_grade = _oracle_probe_grade(
                    {
                        "status": oracle_outcome.status,
                        "detail": getattr(oracle_outcome, "detail", None),
                        "checks": getattr(oracle_outcome, "checks", None),
                    },
                    ok=oracle_outcome.status == "success",
                    status=str(oracle_outcome.status),
                )
            except Exception as error:  # noqa: BLE001 - graded, not raised
                start_oracle_grade = (
                    False,
                    "validate_error",
                    {"error": f"{type(error).__name__}: {error}"},
                )
        warm_full_ms = before.elapsed_ms
        # The duplicate fast restore exists only to measure fast-vs-full.  It does
        # not contribute to E's task outcome, so formal trials skip it by default.
        fast = (
            session.fast_restore(generated_spec, actions=[], assertions=[])
            if restore_diagnostics
            else None
        )
        fast_ms = fast.elapsed_ms if fast is not None and fast.status == "success" else None
        timing = {
            "cold_restore_ms": cold_ms,
            "warm_restore_ms": warm_full_ms,
            "fast_restore_ms": fast_ms,
            "session_bootstrap_ms": session.bootstrap_ms,
            "speedup": round(cold_ms / max(1, warm_full_ms), 3) if cold_ms else None,
            "fast_vs_full_speedup": round(warm_full_ms / max(1, fast_ms), 3) if fast_ms else None,
            "fast_restore_used": fast is not None and fast.status == "success",
            "fast_restore_fallback": fast is not None and fast.status == "validate_failed",
            "cache_hit": fast is not None,
        }
        result["restore_timing"] = timing
        result["screenshot_before"] = before_shot
        result["initial_state_fingerprint"] = _image_fingerprint(before_shot)
        semantic_ok, semantic_status, semantic_detail = merge_start_semantic(
            restore_ok=before.status == "success",
            restore_status=before.status,
            restore_detail=_phase_detail(before),
            oracle_grade=start_oracle_grade,
        )
        _mark_state_start_semantic(
            result,
            ok=semantic_ok,
            status=semantic_status,
            detail=semantic_detail,
        )
        if not semantic_ok:
            _freeze_pair_start_semantic_failure(
                result,
                task=task,
                accepted_spec=generated_spec,
                synthesis_agent=agent,
                live_before=str(before_shot) if before_shot else None,
            )
            result["status"] = "failure"
            result["reason"] = (
                (before.detail or "luanti before-state restore failed")
                if before.status != "success"
                else (
                    "restored Start does not satisfy the registered oracle "
                    f"target ({semantic_status})"
                )
            )
            result["iterations"] = [
                {
                    "iteration": 1,
                    "result": False,
                    "status": (
                        "restore_failure"
                        if before.status != "success"
                        else "semantic_failure"
                    ),
                    "restore_timing": timing,
                }
            ]
            _set_state_method_timing(
                result,
                time_to_start_ms=before.elapsed_ms,
                time_start_to_goal_ms=0,
            )
            return result

        if not _informative_screenshot(before_shot):
            _mark_state_start_visual_incomplete(result)
            result.update(
                status="failure",
                reason="luanti before-state screenshot is missing or non-informative",
            )
            _set_state_method_timing(
                result,
                time_to_start_ms=before.elapsed_ms,
                time_start_to_goal_ms=0,
            )
            return result

        start_ok, before_verdict = _run_state_start_visual_gate(
            result,
            live_before=str(before_shot),
            goldens=goldens,
            task=task,
            runtime=session,
            accepted_spec=generated_spec,
            synthesis_agent=agent,
        )
        if not start_ok:
            result.update(
                status="failure",
                reason="luanti restored Start failed visual grading",
                iterations=[
                    {
                        "iteration": 1,
                        "result": False,
                        "status": "start_gate_failure",
                        "restore_timing": timing,
                        "screenshot_before": before_shot,
                        "pair_verdict": before_verdict,
                    }
                ],
            )
            _set_state_method_timing(
                result,
                time_to_start_ms=before.elapsed_ms,
                time_start_to_goal_ms=0,
            )
            return result

        transition_spec = _plan_minimal_smoke_transition(
            result,
            planner=minimal_planner,
            base=base,
            task=task,
            generated_spec=generated_spec,
            task_dir=version_dir,
            seed=seed,
        )
        if transition_spec is None:
            return result
        # Candidate after: full synth + smoke + screenshot. The transition is the
        # model's; the goal assertions are the registered oracle's.
        _mark_transition_action_execution_started(result)
        after = session.restore(
            transition_spec,
            actions=transition_spec.get("actions", []),
            assertions=copy.deepcopy(semantic_assertions(oracle_spec) or []),
        )
        after_shot = session.screenshot("after-v1.png")
        after_timing = {
            "warm_restore_ms": after.elapsed_ms,
            "fast_restore_ms": None,
            "fast_restore_used": False,
            "fast_restore_fallback": False,
            "session_bootstrap_ms": session.bootstrap_ms,
        }
        result["after_timing"] = after_timing
        result["screenshot_after"] = after_shot
        _mark_state_goal_semantic(
            result,
            ok=after.status == "success",
            status=after.status,
            detail=_phase_detail(after),
        )
        if after.status != "success":
            result["status"] = "failure"
            result["reason"] = after.detail or "luanti after-state smoke failed"
            result["iterations"] = [
                {
                    "iteration": 1,
                    "result": False,
                    "status": "smoke_failure",
                    "restore_timing": timing,
                    "after_timing": after_timing,
                }
            ]
            _set_state_method_timing(
                result,
                time_to_start_ms=before.elapsed_ms,
                time_start_to_goal_ms=after.elapsed_ms,
            )
            return result

        if not _informative_screenshot(after_shot):
            _mark_state_goal_visual_incomplete(result)
            result.update(
                status="failure",
                reason="luanti after-state screenshot is missing or non-informative",
            )
            _set_state_method_timing(
                result,
                time_to_start_ms=before.elapsed_ms,
                time_start_to_goal_ms=after.elapsed_ms,
            )
            return result

        grader_before_ms = int(result.get("grader_ms", 0) or 0)
        verdict = _run_state_goal_visual_grade(
            result,
            before_verdict=before_verdict,
            live_before=str(before_shot),
            live_after=str(after_shot),
            goldens=goldens,
            task=task,
        )
        visual_grader_ms = int(result.get("grader_ms", 0) or 0) - grader_before_ms
        result["status"] = "success" if result["result"] else "failure"
        result["iterations"] = [
            {
                "iteration": 1,
                "result": result["result"],
                "status": result["status"],
                "restore_timing": timing,
                "after_timing": after_timing,
                "screenshot_before": before_shot,
                "screenshot_after": after_shot,
                "pair_verdict": verdict,
                "grader_ms": visual_grader_ms,
            }
        ]
        _set_state_method_timing(
            result,
            time_to_start_ms=before.elapsed_ms,
            time_start_to_goal_ms=after.elapsed_ms,
        )
        return result
    finally:
        session.close()


def _summarize(results: list[dict[str, Any]], *, seeds: int = 1) -> dict[str, Any]:
    k = max(1, seeds)
    summary: dict[str, Any] = {
        "total_tasks": len(results),
        "passed": 0,
        "failed": 0,
        "unsupported": 0,
        "errors": 0,
        "speedups": [],
        "mean_speedup": None,
        "fast_vs_full_speedups": [],
        "mean_fast_vs_full_speedup": None,
        "fast_restore_used": 0,
        "fast_restore_fallbacks": 0,
        # HumanEval unbiased pass@k (Chen et al. 2021): floats in [0,1], not counts.
        "k": k,
        "pass_at_1": None,
        "pass_at_k": None,
        "pass_at_k_tasks": 0,
        "tasks_passed": 0,
        "cost": {},
    }
    speedups: list[float] = []
    fast_speedups: list[float] = []
    pass_at_1_vals: list[float] = []
    pass_at_k_vals: list[float] = []
    undersampled: list[str] = []
    for result in results:
        # Fast-restore metrics come from the before-pass (restore_timing), where the
        # fast-vs-full comparison is like-for-like (no smoke). after_timing only notes
        # whether the operational after-pass also used the fast path.
        before_timing = result.get("restore_timing") if isinstance(result.get("restore_timing"), dict) else {}
        if before_timing.get("fast_restore_used"):
            summary["fast_restore_used"] += 1
        if before_timing.get("fast_restore_fallback"):
            summary["fast_restore_fallbacks"] += 1
        fast_vs_full = before_timing.get("fast_vs_full_speedup")
        if isinstance(fast_vs_full, (int, float)):
            fast_speedups.append(float(fast_vs_full))
        status = result.get("status")
        if result.get("result"):
            summary["passed"] += 1
            summary["tasks_passed"] += 1
        elif status == "unsupported":
            summary["unsupported"] += 1
        elif status == "failed":
            summary["errors"] += 1
        else:
            summary["failed"] += 1
        # HumanEval pass@k from per-task sample counts (n=seed_count, c=pass_count).
        # seeds=1 tasks are not aggregated, so fall back to n=1, c=1 if result else 0.
        n_i = int(result.get("seed_count", 1))
        c_i = int(result.get("pass_count", 1 if result.get("result") else 0))
        pass_at_1_vals.append(_pass_at_k(n_i, c_i, 1))
        if n_i >= k:
            pass_at_k_vals.append(_pass_at_k(n_i, c_i, k))
        else:
            undersampled.append(str(result.get("task_id")))
        timing = result.get("restore_timing") if isinstance(result.get("restore_timing"), dict) else {}
        speedup = timing.get("speedup")
        if isinstance(speedup, (int, float)):
            speedups.append(float(speedup))
    summary["speedups"] = speedups
    if speedups:
        summary["mean_speedup"] = round(sum(speedups) / len(speedups), 3)
    summary["fast_vs_full_speedups"] = fast_speedups
    if fast_speedups:
        summary["mean_fast_vs_full_speedup"] = round(sum(fast_speedups) / len(fast_speedups), 3)
    if pass_at_1_vals:
        summary["pass_at_1"] = round(sum(pass_at_1_vals) / len(pass_at_1_vals), 4)
    summary["pass_at_k_tasks"] = len(pass_at_k_vals)
    if pass_at_k_vals:
        summary["pass_at_k"] = round(sum(pass_at_k_vals) / len(pass_at_k_vals), 4)
    if undersampled:
        # Explicitly surfaced rather than silently degrading pass@k to pass@n_i.
        summary["pass_at_k_undersampled"] = undersampled
    summary["cost"] = _sum_costs([result.get("cost") for result in results])
    return summary


def _pass_at_k_markdown_line(summary: dict[str, Any]) -> str:
    k = summary.get("k", 1)
    pak = summary.get("pass_at_k")
    pak_str = "N/A" if pak is None else f"{pak}"  # avoid printing Python None
    total = summary.get("total_tasks", 0)
    line = (
        f"- pass@1 `{summary.get('pass_at_1')}` | pass@{k} `{pak_str}` "
        f"(over `{summary.get('pass_at_k_tasks', 0)}/{total}` tasks) | "
        f"tasks passed `{summary.get('tasks_passed', 0)}/{total}`"
    )
    under = summary.get("pass_at_k_undersampled")
    if under:
        line += f"\n- pass@{k} undersampled tasks (n<{k}, excluded): `{', '.join(under)}`"
    return line


def _render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# State Loop Benchmark",
        "",
        f"- Generated: `{payload.get('generated_at_utc')}`",
        f"- Synthesis agent: `{payload.get('synthesis_agent')}`",
        f"- Synthesis model: `{payload.get('synthesis_model') or ''}`",
        f"- Tasks: `{summary.get('total_tasks', 0)}`",
        _pass_at_k_markdown_line(summary),
        f"- Mean restore speedup (cold/warm): `{summary.get('mean_speedup')}`",
        f"- Fast-restore: used `{summary.get('fast_restore_used', 0)}`, fallbacks `{summary.get('fast_restore_fallbacks', 0)}`, "
        f"mean fast-vs-full speedup `{summary.get('mean_fast_vs_full_speedup')}`",
        f"- LLM cost: `{summary.get('cost', {}).get('llm_calls', 0)}` calls, "
        f"`{summary.get('cost', {}).get('total_tokens', 0)}` tokens, "
        f"est_usd `{summary.get('cost', {}).get('est_usd')}`",
        f"- Claim boundary: {payload.get('claim_boundary')}",
        "",
        "## Results",
        "| task | benchmark | result | cold_ms | warm_ms | speedup | bootstrap_ms |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in payload.get("results", []):
        timing = result.get("restore_timing") if isinstance(result.get("restore_timing"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result.get("task_id")),
                    str(result.get("benchmark")),
                    "PASS" if result.get("result") else str(result.get("status")),
                    str(timing.get("cold_restore_ms")),
                    str(timing.get("warm_restore_ms")),
                    str(timing.get("speedup")),
                    str(result.get("session_bootstrap_ms")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"
