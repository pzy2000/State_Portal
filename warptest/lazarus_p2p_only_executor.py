"""P2P-only trial executor with the track's own upstream evidence contract."""

from __future__ import annotations

import json
import re
import threading
import time
from contextlib import contextmanager, nullcontext
from . import cost
from .c1_task_deadline import C1TaskTimeout, bounded_task_timeout_s
from dataclasses import replace
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from .lazarus_phase0 import LazarusPhase0CellContext
from .lazarus_trial import (
    LazarusRuntimeObservation,
    LazarusVisualEvidence,
    judge_lazarus_visual_evidence,
    run_lazarus_trial,
)
from .lazarus_upstream import (
    PER_TRIAL_P2P_TIMEOUT_S,
    UpstreamTestDefinition,
    definitions_from_upstream_record,
    run_lazarus_per_trial_p2p,
)

P2P_CONTROL_SLOTS = 2
# Eight lanes can wait behind three full waves of two controls. Admission is
# operational overhead, separate from each unchanged 300-second subset budget.
P2P_ADMISSION_TIMEOUT_S = 3 * PER_TRIAL_P2P_TIMEOUT_S


def build_lazarus_p2p_only_executor(
    root: str | Path,
    *,
    runtime_runner: Any,
    p2p_sandbox_root: str | Path,
    judge_model: str = "qwen3-vl-flash@relay",
    attempt_log_root: str | Path | None = None,
) -> Any:
    """Bind one online runtime to visual grading and P2P-only controls.

    The strict ``lz-*`` trial-provenance validator requires frozen F2P source
    capsules.  ``lzp-*`` rows intentionally have no F2P and instead publish a
    Gate1-6 record plus a repository control path.  This executor validates that
    disjoint shape directly rather than weakening the strict validator.
    """

    workspace = Path(root).expanduser().resolve()
    sandbox = _contained(workspace, p2p_sandbox_root, "P2P sandbox")
    # Eight policy lanes can otherwise launch eight uncached Jest suites at
    # once. Bound only local control execution; model/runtime lanes stay intact.
    p2p_slots = threading.BoundedSemaphore(P2P_CONTROL_SLOTS)

    from .lazarus_p2p_only_recovery import atomic_receipt, classify_failure
    ledger_root = Path(attempt_log_root) if attempt_log_root is not None else sandbox / "cell-ledgers"

    def execute_unobserved(context: LazarusPhase0CellContext) -> Mapping[str, Any]:
        if not context.key.task_id.startswith("lzp-"):
            raise ValueError("P2P-only executor permits only lzp-* tasks")
        task = context.task
        defect = task.get("defect") if isinstance(task, Mapping) else None
        if not isinstance(defect, Mapping):
            raise ValueError(f"{context.key.task_id}: defect metadata is missing")
        spec_path = _contained(
            workspace,
            str(task.get("oracle_spec") or ""),
            f"{context.key.task_id} oracle",
        )
        spec = _yaml_mapping(spec_path, f"{context.key.task_id} oracle")
        upstream_path = _contained(
            workspace,
            str(defect.get("upstream_record") or ""),
            f"{context.key.task_id} upstream record",
        )
        upstream = _json_mapping(
            upstream_path, f"{context.key.task_id} upstream record"
        )
        definitions = definitions_from_upstream_record(upstream)
        _validate_p2p_only_definitions(
            context,
            definitions,
            source_root=_contained(
                workspace,
                str(task.get("repo") or ""),
                f"{context.key.task_id} source",
            ),
            upstream=upstream,
        )
        definitions = _runtime_definitions(definitions, task=task)
        observation: LazarusRuntimeObservation | None = None

        @contextmanager
        def measured_phase(name: str):
            started = time.monotonic()
            with cost.scoped_meter() as meter:
                try:
                    yield
                finally:
                    if observation is not None and isinstance(observation.trace, dict):
                        ledger = observation.trace.setdefault("costs", {})
                        prior = ledger.get(name, {"duration_ms": 0, "usage": {}})
                        usage = cost.merge_summaries([prior["usage"], meter.summary()])
                        ledger[name] = {
                            "duration_ms": prior["duration_ms"]
                            + int((time.monotonic() - started) * 1000),
                            "usage": usage,
                        }

        def run_runtime() -> LazarusRuntimeObservation:
            nonlocal observation
            observation = (
                runtime_runner.run_cell(context, start_gate=grade_start)
                if hasattr(runtime_runner, "run_cell")
                else runtime_runner(context)
            )
            return observation

        def grade_start(value: LazarusRuntimeObservation) -> LazarusVisualEvidence:
            if value.before_semantic_ok is not True:
                from .lazarus_grading import VisualJudgement
                from .lazarus_p2p_only_identity import file_sha256

                before = VisualJudgement(
                    False, file_sha256(value.before_screenshot) if value.before_screenshot is not None else None, None
                )
                return LazarusVisualEvidence(
                    before,
                    VisualJudgement(None, None, None),
                    VisualJudgement(None, None, None),
                    {"status": "not_evaluated_semantic_start_failed"},
                )
            return judge_lazarus_visual_evidence(
                value,
                spec=spec,
                spec_path=spec_path,
                evaluate_after=False,
                judge_model=judge_model,
            )

        def run_visual(
            value: LazarusRuntimeObservation, evaluate_after: bool
        ) -> LazarusVisualEvidence:
            prior = value.start_gate_visual
            if prior is not None and (
                prior.before.ok is not True or not evaluate_after
            ):
                return prior
            kwargs = {}
            if prior is not None:
                kwargs["prior_before"] = prior.audits["fixed"]["before"]
            with measured_phase("grading"):
                return judge_lazarus_visual_evidence(
                    value,
                    spec=spec,
                    spec_path=spec_path,
                    evaluate_after=evaluate_after,
                    judge_model=judge_model,
                    **kwargs,
                )

        def run_p2p() -> Mapping[str, Any]:
            if observation is None or observation.p2p_restore_adapter is None:
                raise RuntimeError("P2P-only runtime did not bind a Start restorer")
            with _p2p_admission(p2p_slots) as wait_ms, measured_phase("p2p"):
                if isinstance(observation.trace, dict):
                    observation.trace["p2p_admission"] = {
                        "max_concurrent_controls": P2P_CONTROL_SLOTS,
                        "wait_ms": wait_ms,
                    }
                return run_lazarus_per_trial_p2p(
                    task_id=context.key.task_id,
                    version=context.key.version,
                    checkpoint_sha256=observation.checkpoint_sha256,
                    checkpoint_kind=(
                        "semantic_state"
                        if context.key.arm
                        in {"lazarus_state", "lazarus_agent_s_synth", "lazarus_scalecua_synth"}
                        else (
                            "scalecua_from_start"
                            if context.key.arm == "lazarus_scalecua"
                            else "agent_s_from_start"
                        )
                    ),
                    source_root=context.artifact.root,
                    definitions=definitions,
                    test_ids=context.per_trial_p2p_ids,
                    sandbox_root=sandbox / context.key.invocation_id(),
                    harness_root=workspace,
                    trusted_git_dir=context.artifact.git_metadata,
                    checkpoint_restorer=observation.p2p_restore_adapter,
                )

        errors = []
        def capture(callback):
            def wrapped(*args, **kwargs):
                try:
                    return callback(*args, **kwargs)
                except Exception as error:
                    errors.append(classify_failure(error))
                    raise
            return wrapped

        row = run_lazarus_trial(
            key=context.key,
            task_id=context.key.task_id,
            defect_locus="at_goal",
            artifact=context.artifact,
            per_trial_p2p_ids=context.per_trial_p2p_ids,
            version_switch_ms=context.version_switch_ms,
            runtime_runner=capture(run_runtime),
            visual_runner=capture(run_visual),
            p2p_runner=capture(run_p2p),
            grading_mode="p2p_only_visual",
        )

        if errors:
            row["infrastructure_failure"] = next((error for error in errors if not error["retryable"]), errors[0])
        return row

    def execute(context: LazarusPhase0CellContext) -> Mapping[str, Any]:
        receipt_path = ledger_root / (context.key.invocation_id() + ".json")
        if receipt_path.exists():
            raise ValueError("immutable cell provider ledger already exists; use a fresh attempt root")
        row = None
        failure = None
        with cost.observe_calls() as audit_meter, cost.scoped_meter():
            try:
                row = dict(execute_unobserved(context))
            except (Exception, C1TaskTimeout) as error:
                failure = _accounted_failure(error, audit_meter.summary())
                error.recovery = failure
                raise
            finally:
                usage = audit_meter.summary()
                atomic_receipt(receipt_path, {
                    "schema": "lazarus-cell-provider-ledger-v1",
                    "key": {name: getattr(context.key, name) for name in ("task_id", "model", "seed", "arm", "version")},
                    "usage": usage, "failure": failure or (row or {}).get("infrastructure_failure"),
                })
        phase_failure = _recorded_provider_failure(row)
        if phase_failure is not None:
            row["infrastructure_failure"] = phase_failure
            row["complete"] = False
        if row.get("infrastructure_failure") is None:
            p2p_failure = _recorded_p2p_timeout(row)
            if p2p_failure is not None:
                row["infrastructure_failure"] = p2p_failure
                row["complete"] = False
        runtime_failure = _local_runtime_timeout(row, usage)
        if runtime_failure is not None:
            row["infrastructure_failure"] = runtime_failure
        # Full prompts/responses live once in the sidecar, not in every summary.
        row["actual_provider_usage"] = {name: value for name, value in usage.items() if name != "calls"}
        row["actual_provider_ledger"] = str(receipt_path.resolve())
        if usage.get("usage_complete") is False:
            row["infrastructure_failure"] = {"retryable": False, "category": "usage_unknown", "error": "provider usage is incomplete"}
            row["complete"] = False
        return row

    execute.resource_scope = getattr(
        runtime_runner, "resource_scope", lambda context: nullcontext()
    )
    return execute


def _validate_p2p_only_definitions(
    context: LazarusPhase0CellContext,
    definitions: Mapping[str, UpstreamTestDefinition],
    *,
    source_root: Path,
    upstream: Mapping[str, Any],
) -> None:
    if not isinstance(upstream.get("f2p_absence"), Mapping):
        raise ValueError(f"{context.key.task_id}: F2P absence evidence is missing")
    selected = tuple(context.per_trial_p2p_ids)
    if not selected or len(selected) != len(set(selected)):
        raise ValueError(f"{context.key.task_id}: per-trial P2P ids are invalid")
    if set(selected) - set(definitions):
        raise ValueError(f"{context.key.task_id}: per-trial P2P definition is missing")
    raw_tests = upstream.get("tests")
    if not isinstance(raw_tests, list):
        raise ValueError(f"{context.key.task_id}: P2P-only test rows are missing")
    raw_by_id = {
        str(value.get("id") or ""): value
        for value in raw_tests
        if isinstance(value, Mapping)
    }
    for test_id in selected:
        raw = raw_by_id.get(test_id)
        if not isinstance(raw, Mapping):
            raise ValueError(f"{test_id}: P2P-only raw control row is missing")
        if raw.get("kind") != "pass_to_pass":
            raise ValueError(f"{test_id}: P2P-only control kind is invalid")
        relative = raw.get("path")
        if not isinstance(relative, str) or not relative:
            raise ValueError(f"{test_id}: P2P-only control path is missing")
        path = (source_root / relative).resolve()
        if source_root != path and source_root not in path.parents:
            raise ValueError(f"{test_id}: P2P-only control path escapes source")
        if not path.is_file():
            raise ValueError(f"{test_id}: P2P-only control path is missing")


def _runtime_definitions(
    definitions: Mapping[str, UpstreamTestDefinition],
    *,
    task: Mapping[str, Any],
) -> dict[str, UpstreamTestDefinition]:
    """Resolve Jupyter's root-installed Jest from a package working directory."""

    if str(task.get("benchmark") or "") != "jupyterlab":
        return dict(definitions)
    rewritten: dict[str, UpstreamTestDefinition] = {}
    for test_id, definition in definitions.items():
        argv = definition.argv
        if (
            len(argv) >= 2
            and argv[0] == "node"
            and argv[1] == "node_modules/jest/bin/jest.js"
            and definition.cwd.startswith("packages/")
        ):
            argv = (argv[0], "../../node_modules/jest/bin/jest.js", *argv[2:])
        rewritten[test_id] = replace(definition, argv=argv)
    return rewritten


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


def _json_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


def _yaml_mapping(path: Path, label: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f"{label} cannot be loaded: {error}") from error
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return value


__all__ = ["build_lazarus_p2p_only_executor"]


def _recorded_provider_failure(row: Mapping[str, Any]) -> dict | None:
    from .lazarus_p2p_only_recovery import classify_failure
    # Agent-S persists policy exceptions as phase action diagnostics rather
    # than propagating them. Recover only explicit provider rejection/outage
    # evidence from those coordinator-written diagnostics, never a plain timeout.
    trace = (row.get("runtime") or {}).get("trace") or {}
    trajectory = trace.get("trajectory") or {}
    artifact_dir = trajectory.get("artifact_dir")
    phase_failures = []
    if isinstance(artifact_dir, str):
        from .run_context import provider_capacity_failure
        for phase in ("phase-a", "phase-b"):
            actions_path = Path(artifact_dir) / phase / "actions.json"
            if not actions_path.is_file():
                continue
            for action in json.loads(actions_path.read_text()).get("actions", []):
                message = str(action.get("error") or "")
                error = RuntimeError(message)
                capacity = provider_capacity_failure(message)
                if capacity:
                    error.provider_capacity = capacity
                elif action.get("infrastructure_error") is True:
                    # SDK errors use "Error code: 500" and repr/JSON bodies
                    # quote status keys. Parse only coordinator-marked errors.
                    status = re.search(r"(?:HTTP(?: Error)?[ :]+|Error code[ :]+|['\"]?status(?:_code)?['\"]?[ =:]+)(400|401|403|404|422|429|5[0-9]{2})\b", message, re.I)
                    if status:
                        error.status_code = int(status.group(1))
                    elif re.match(r"^(?:APIConnectionError|ConnectError|ConnectTimeout):", message):
                        error = ConnectionError(message)
                    elif any(label in message.lower() for label in ("rate limit", "ratelimit", "too many requests", "gateway unavailable")):
                        error.artifacts = {"failure_mode": "gateway_outage"}
                disposition = classify_failure(error)
                if disposition["category"] != "infrastructure_or_integrity":
                    phase_failures.append(disposition)
    if phase_failures:
        return next((item for item in phase_failures if not item["retryable"]), phase_failures[0])
    return None


@contextmanager
def _p2p_admission(slots):
    started = time.monotonic()
    if not slots.acquire(timeout=bounded_task_timeout_s(P2P_ADMISSION_TIMEOUT_S)):
        error = TimeoutError("local P2P control admission timed out")
        error.recovery = {"retryable": True, "category": "p2p_admission_timeout", "error": str(error)}
        raise error
    try:
        yield int((time.monotonic() - started) * 1000)
    finally:
        slots.release()


def _recorded_p2p_timeout(row: Mapping[str, Any]) -> dict | None:
    """Retry explicit local test timeouts, never assertions or missing evidence."""
    evidence = row.get("per_trial_p2p_evidence") or {}
    if not isinstance(evidence, Mapping):
        return None
    results = evidence.get("results")
    if (
        evidence.get("completed") is not True
        or evidence.get("passed") is not False
        or not isinstance(results, list)
        or not results
        or any(not isinstance(item, Mapping) or not item.get("id") for item in results)
    ):
        return None
    failed = [item for item in results if item.get("passed") is not True]
    if not failed or any(
        item.get("timed_out") is not True or item.get("exit_code") is not None
        for item in failed
    ):
        return None
    allowed = {"P2P subset failed"} | {
        f"per-trial P2P did not pass for {item['id']}" for item in failed
    }
    if not row.get("failures") or not set(row["failures"]).issubset(allowed):
        return None
    return {"retryable": True, "category": "p2p_control_timeout", "error": "; ".join(evidence.get("failures") or ["local P2P test timed out"])}


# ScaleCUA requests run inside the runtime phase too, but a stalled worker may
# have generated tokens off-ledger, so those stay fail-closed by name.
_PROVIDER_STALL = re.compile(r"ScaleCUA (?:worker|provider)", re.I)
_TIMEOUT_SHAPE = re.compile(r"timed out|timeout", re.I)


def _local_runtime_timeout(row: Mapping[str, Any], usage: Mapping[str, Any]) -> dict | None:
    """Retry a runtime-phase stall whose provider spend is fully accounted.

    ``run_lazarus_trial`` records which phase raised, so a disposition recovers
    only when the terminal error is the one the local runtime runner reported.
    Binding to that phase replaces enumerating every capture, engine-IPC and
    cold-start call site, and complete accounting is the bar the task-deadline
    recovery already applies, so the replay cannot hide spend.
    """

    failure = row.get("infrastructure_failure")
    if (
        not isinstance(failure, Mapping)
        or failure.get("retryable") is not False
        or failure.get("category") != "infrastructure_or_integrity"
        or usage.get("usage_complete") is not True
    ):
        return None
    error = str(failure.get("error") or "")
    if (
        f"runtime execution failed: {error}" not in (row.get("failures") or ())
        or not _TIMEOUT_SHAPE.search(error)
        or _PROVIDER_STALL.search(error)
    ):
        return None
    return {"retryable": True, "category": "local_runtime_timeout", "error": error}


def _accounted_failure(error: BaseException, usage: Mapping[str, Any]) -> dict:
    from .lazarus_p2p_only_recovery import classify_failure
    from .run_context import safe_error_text
    if usage.get("usage_complete") is False:
        return {"retryable":False,"category":"usage_unknown","error":safe_error_text(error)}
    if isinstance(error, C1TaskTimeout) and usage.get("usage_complete") is True:
        return {"retryable":True,"category":"task_deadline_with_known_usage","error":safe_error_text(error)}
    # A runtime-phase stall never reaches here: run_lazarus_trial absorbs it and
    # reports the phase on the row, where _local_runtime_timeout can recover it.
    return classify_failure(error)
