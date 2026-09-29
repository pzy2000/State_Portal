"""C/D/E generated-state runtime with a coordinator-owned pre-transition gate."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import cost
from .c1_task_deadline import raise_if_task_timed_out
from .c1_runtime_hold import hold_runtime_state_for_start_grading
from .gui_agent import _build_policy_client, _run_phase
from .lazarus_p2p_only_contract import (
    C,
    D,
    E,
    F,
    G,
    METHODS,
    METHOD_PROTOCOL,
    SYNTH_ARMS,
    method_fields,
    validate_method_fields,
)
from .lazarus_p2p_only_generated_runtime import (
    GeneratedJupyterSession,
    validate_observed_start,
)
from .lazarus_p2p_only_identity import (
    canonical_sha256,
    file_sha256,
    treatment_runtime_sha256,
)
from .lazarus_p2p_only_native import (
    GeneratedLuantiSession,
    generated_openra_session,
    prepare_generated_openra_tree,
)
from .lazarus_p2p_only_synthesis import obtain_shared_synthesis
from .lazarus_runtime import (
    _agent_s_reference_steps_prompt,
    _agent_s_start_instruction,
    _managed_agent_s_client,
    _persist_phase_actions,
    _phase_steps,
    _phase_trace,
)
from .lazarus_trial import LazarusRuntimeObservation
from .visual_bridge import FixedVisualBridge

_NATIVE_INPUT_LOCK = threading.RLock()


class P2POnlyPaidRuntimeRunner:
    def __init__(
        self,
        *,
        workspace: Path,
        runtime_root: Path,
        provider_timeout_s: float = 900,
        max_steps: int = 100,
        method_config: dict | None = None,
    ):
        self.workspace = workspace.resolve()
        self.runtime_root = runtime_root.resolve()
        self.provider_timeout_s = float(provider_timeout_s)
        if max_steps != 100:
            raise ValueError("v3 requires 100 decision steps")
        self.config = method_fields() if method_config is None else method_config
        arms = tuple(self.config.get("methods") or ())
        validate_method_fields(self.config, arms=arms)
        self.max_steps = max_steps
        self._start_gates = {}
        self._runtime_identity = treatment_runtime_sha256(self.workspace)

    def resource_scope(self, context):
        return (
            _NATIVE_INPUT_LOCK
            if context.task["benchmark"] in {"luanti", "openra"}
            else nullcontext()
        )

    def _synthesis(self, context):
        return obtain_shared_synthesis(
            context,
            cache_root=self.runtime_root / "shared-v3",
            config=self.config["shared_synthesis"],
        )

    def __call__(self, context):
        raise RuntimeError(
            "v3 runtime requires the coordinator start_gate callback; use run_cell"
        )

    def _client(self, context):
        config = self.config["methods"][context.key.arm]
        return _build_policy_client(
            policy_kind=config["policy_kind"],
            model=context.key.model,
            base_url=os.environ["RELAY_BASE_URL"],
            api_key_env="RELAY_API_KEY",
            temperature=config["temperature"],
            grounder_model=context.key.model,
            provider_timeout_s=self.provider_timeout_s,
            root=self.workspace,
        )

    def _phase(self, session, client, context, output, phase, budget):
        config = self.config["methods"][context.key.arm]
        observer = FixedVisualBridge(
            model=config["observer_model"],
            base_url=os.environ["RELAY_BASE_URL"],
            api_key_env="RELAY_API_KEY",
            provider_timeout_s=self.provider_timeout_s,
        )
        prompt = (
            _agent_s_start_instruction(context)
            if phase == "phase-a"
            else _agent_s_reference_steps_prompt(context)
        )
        if phase == "phase-a":
            from .lazarus_p2p_only_contract import public_start_constraint
            constraint = public_start_constraint(context.key.task_id)
            if constraint:
                prompt += "\n\n" + constraint
        result = _run_phase(
            runtime=session,
            client=client,
            instruction=prompt,
            phase_dir=output / phase,
            max_steps=budget,
            observer=observer,
            task_id=context.key.task_id,
        )
        _persist_phase_actions(result, output / phase)
        return result

    def run_cell(self, context: Any, *, start_gate: Any) -> LazarusRuntimeObservation:
        key = context.key
        benchmark = context.task["benchmark"]
        if key.arm not in METHODS:
            raise ValueError("unknown v3 arm")
        output = self.runtime_root / benchmark / key.invocation_id()
        output.mkdir(parents=True, exist_ok=True)
        shared = None
        cache_hit = None
        if key.arm in SYNTH_ARMS:
            shared, cache_hit = self._synthesis(context)
        costs = {
            name: {"duration_ms": 0, "usage": cost.CostMeter().summary()}
            for name in ("synthesis", "restore", "execution", "bootstrap", "grading")
        }
        if shared:
            costs["synthesis"] = {
                "duration_ms": 0 if cache_hit else shared["duration_ms"],
                "usage": cost.CostMeter().summary() if cache_hit else shared["usage"],
                "synthesis_id": shared["synthesis_id"],
                "shared_actual": {
                    "duration_ms": shared["duration_ms"],
                    "usage": shared["usage"],
                },
                "attributed": {
                    "duration_ms": shared["duration_ms"],
                    "usage": shared["usage"],
                },
            }
        started = time.monotonic()
        with self._session(
            context, benchmark=benchmark, output=output / "session"
        ) as session:
            costs["bootstrap"]["duration_ms"] = int((time.monotonic() - started) * 1000)
            initial = Path(session.capture_policy_frame(output / "initial.png"))
            initial_state = self._observe(session)
            phase_a = {}
            phase_b = {"executed": False, "steps": 0, "actions": []}
            failure = None
            policy_incomplete = False
            start_state = None
            budget = self.max_steps
            restore_start = time.monotonic()
            with cost.scoped_meter() as restore_meter:
                if key.arm in (C, F):
                    with _managed_agent_s_client(self._client(context)) as client:
                        result = self._phase(
                            session, client, context, output, "phase-a", budget
                        )
                    phase_a = _phase_trace(result)
                    budget = max(0, budget - _phase_steps(result))
                    if getattr(result, "incomplete", False):
                        failure = "phase_a_incomplete"
                        policy_incomplete = True
                elif shared["usable"]:
                    try:
                        # Restore only the generated state; actions are never passed to this interface.
                        session.restore_generated_state(
                            shared["generated_spec"]["target"]["state"]
                        )
                        if (
                            session.export_state()
                            != shared["generated_spec"]["target"]["state"]
                        ):
                            raise RuntimeError(
                                "generated state restore attestation drift"
                            )
                        phase_a = {"kind": "generated_state_restore", "actions": []}
                    except (ValueError, RuntimeError) as error:
                        if any(
                            token in str(error).lower()
                            for token in (
                                "timed out",
                                "exited",
                                "ipc",
                                "bridge build",
                                "attestation drift",
                            )
                        ):
                            raise
                        failure = "restore_failed: " + str(error)
                else:
                    failure = "synthesis_failed: " + str(shared["failure"])
            costs["restore"] = {
                "duration_ms": int((time.monotonic() - restore_start) * 1000),
                "usage": restore_meter.summary(),
            }
            policy_exit = (
                self._phase_a_policy_exit(session, result)
                if key.arm in (C, F) and benchmark == "luanti"
                else None
            )
            if policy_exit is not None:
                evidence = {"passed": False, "runtime_healthy": False,
                    "failure": "policy_closed_application_before_start",
                    "source": "accepted_native_input_and_process_exit", "policy_exit": policy_exit}
                observation = LazarusRuntimeObservation(
                    complete=True, runtime_restored=True, before_semantic_ok=False,
                    goal_checks_ok=None, defect_signature_ok=None, invariants_ok=False,
                    before_screenshot=None, after_screenshot=None,
                    checkpoint_sha256=canonical_sha256(evidence), synthesis_cache_hit=None,
                    actual_synthesis_calls=0, checkpoint_reuse_success=None,
                    treatment_tokens=int(costs["restore"]["usage"].get("total_tokens") or 0),
                    treatment_time_ms=costs["restore"]["duration_ms"],
                    initial_screenshot=initial,
                    start_construction_mode=(
                        "scalecua_phase_a_from_cold_start"
                        if key.arm == F
                        else "agent_s_phase_a_from_cold_start"
                    ),
                    start_semantic_evidence=evidence, start_evaluated=True,
                    trace={"schema":"p2p-only-cde-runtime-v3", "method_protocol":METHOD_PROTOCOL,
                        "benchmark":benchmark, "artifact_dir":str(output),
                        "trajectory":{"artifact_dir":str(output),"recorded":True},
                        "policy_exit":policy_exit, "phase_a":phase_a,
                        "phase_b":{"executed":False,"not_run_reason":"policy_closed_application_before_start"},
                        "start_gate":{"semantic":False,"visual":None,"passed":False}, "costs":costs},
                )
                return replace(observation, start_gate_visual=start_gate(observation))
            observed = self._observe(session)
            semantic = failure is None and validate_observed_start(
                context.task, observed
            )
            before = self._capture(
                session, output / "before.png", benchmark, allow_policy=True
            )
            if semantic:
                start_state = session.export_state()
            checkpoint_sha = (
                shared["checkpoint_sha256"]
                if shared
                else canonical_sha256(start_state or observed)
            )
            start_evidence = {
                "passed": semantic,
                "observed": observed,
                "failure": failure,
                "already_satisfied": semantic and initial_state == observed,
                "source": "independent_public_preconditions_v3",
            }
            trace = {
                "schema": "p2p-only-cde-runtime-v3",
                "method_protocol": METHOD_PROTOCOL,
                "artifact_dir": str(output),
                "initial_state": initial_state,
                "same_live_lineage": True,
                "trajectory": {"artifact_dir": str(output), "recorded": True},
                "phase_a": phase_a,
                "phase_b": phase_b,
                "costs": costs,
                "policy_action_interface": (
                    "pixel_mouse_keyboard_only"
                    if key.arm in (C, D, F, G)
                    else "generated_semantic_actions"
                ),
                "synthesis": {
                    "cache_hit": cache_hit,
                    "usable": shared["usable"],
                    "synthesis_id": shared["synthesis_id"],
                    "generated_spec": shared["generated_spec"],
                    "artifact_dir": shared["artifact_dir"],
                    "trace": {
                        "artifact_dir": shared["artifact_dir"],
                        "cost_ledger": "cost-ledger.json",
                    },
                }
                if shared
                else None,
            }
            observation = LazarusRuntimeObservation(
                complete=True,
                runtime_restored=True,
                before_semantic_ok=semantic,
                goal_checks_ok=None,
                defect_signature_ok=None,
                invariants_ok=False,
                before_screenshot=before,
                after_screenshot=None,
                checkpoint_sha256=checkpoint_sha,
                synthesis_cache_hit=cache_hit,
                actual_synthesis_calls=0
                if not shared or cache_hit
                else int(shared["usage"]["llm_calls"]),
                checkpoint_reuse_success=cache_hit if shared else None,
                treatment_tokens=0,
                treatment_time_ms=0,
                trace=trace,
                initial_screenshot=initial,
                start_construction_mode=(
                    "shared_generated_state_from_cold_start"
                    if shared
                    else (
                        "scalecua_phase_a_from_cold_start"
                        if key.arm == F
                        else "agent_s_phase_a_from_cold_start"
                    )
                ),
                start_semantic_evidence=start_evidence,
                start_evaluated=True,
            )
            gate_key = (key.task_id, key.model, key.seed, key.version)
            gate_path = (
                self.runtime_root
                / "start-gates-v3"
                / (
                    canonical_sha256(
                        {
                            "key": gate_key,
                            "checkpoint": checkpoint_sha,
                            "runtime": self._runtime_identity,
                        }
                    )
                    + ".json"
                )
            )
            if shared and gate_key not in self._start_gates and gate_path.is_file():
                from .lazarus_grading import VisualJudgement
                from .lazarus_trial import LazarusVisualEvidence

                entry = json.loads(gate_path.read_text())
                checksum = entry.pop("entry_sha256", None)
                if (
                    checksum != canonical_sha256(entry)
                    or entry.get("identity") != gate_path.stem
                ):
                    raise RuntimeError("shared Start gate cache integrity failure")
                v = entry["visuals"]
                verdict = LazarusVisualEvidence(
                    VisualJudgement(**v["before"]),
                    VisualJudgement(**v["fixed_after"]),
                    VisualJudgement(**v["buggy_after"]),
                    v["audits"],
                )
                self._start_gates[gate_key] = (
                    entry["state"],
                    entry["semantic"],
                    entry["frame_sha256"],
                    verdict,
                )
            gate_started = time.monotonic()
            with cost.scoped_meter() as grading_meter:
                previous = self._start_gates.get(gate_key) if shared else None
                if previous is not None:
                    prior_state, prior_semantic, prior_frame, visuals = previous
                    if prior_state != start_state or prior_semantic != semantic:
                        raise RuntimeError("D/E local Start restoration drift")
                    from PIL import Image, ImageStat

                    with Image.open(before) as frame:
                        if (
                            min(frame.size) < 32
                            or max(ImageStat.Stat(frame.convert("RGB")).stddev) <= 1
                        ):
                            raise RuntimeError(
                                "D/E local Start screenshot is not informative"
                            )
                    # Image hashes bind evidence, not semantic equivalence. Native backgrounds
                    # and caret/antialiasing can differ despite an identical restored state.
                    trace["shared_before_source_sha256"] = prior_frame
                    visuals = replace(
                        visuals,
                        before=replace(
                            visuals.before, screenshot_sha256=file_sha256(before)
                        ),
                    )
                else:
                    hold = {}
                    with hold_runtime_state_for_start_grading(session, metadata=hold):
                        visuals = start_gate(observation)
                    trace["start_hold"] = hold
                    if shared and semantic:
                        self._start_gates[gate_key] = (
                            start_state,
                            semantic,
                            file_sha256(before),
                            visuals,
                        )
                        entry = {
                            "identity": gate_path.stem,
                            "state": start_state,
                            "semantic": semantic,
                            "frame_sha256": file_sha256(before),
                            "visuals": asdict(visuals),
                        }
                        gate_path.parent.mkdir(parents=True, exist_ok=True)
                        with gate_path.open("x") as gate_file:
                            json.dump(
                                {**entry, "entry_sha256": canonical_sha256(entry)},
                                gate_file,
                                sort_keys=True,
                            )
            costs["grading"] = {
                "duration_ms": int((time.monotonic() - gate_started) * 1000),
                "usage": grading_meter.summary(),
            }
            allowed = semantic and visuals.before.ok is True
            trace["start_gate"] = {
                "semantic": semantic,
                "visual": visuals.before.ok,
                "passed": allowed,
                "shared": previous is not None,
                "version": key.version,
                "before_sha256": file_sha256(before),
            }
            after = None
            execution_started = time.monotonic()
            with cost.scoped_meter() as execution_meter:
                if allowed:
                    if key.arm == E:
                        actions = shared["generated_spec"]["actions"]
                        if (
                            len(actions)
                            > self.config["methods"][E]["semantic_action_limit"]
                        ):
                            raise ValueError("semantic action budget exceeded")
                        phase_b["executed"] = bool(actions)
                        if not actions:
                            phase_b["not_run_reason"] = "no_generated_actions"
                        for action in actions:
                            raise_if_task_timed_out()
                            try:
                                result = session.execute_semantic(action)
                            except Exception as error:
                                if not any(
                                    token in str(error).lower()
                                    for token in (
                                        "selector missing",
                                        "button missing",
                                        "not a ",
                                        "text field missing",
                                        "world is unavailable",
                                        "selection target",
                                        "indexsizeerror",
                                    )
                                ):
                                    raise
                                phase_b["actions"].append(
                                    {
                                        "action": action,
                                        "receipt": {
                                            "accepted": False,
                                            "error": str(error),
                                        },
                                    }
                                )
                                break
                            phase_b["actions"].append(
                                {"action": action, "receipt": result}
                            )
                        phase_b["steps"] = len(phase_b["actions"])
                    elif budget:
                        with _managed_agent_s_client(self._client(context)) as client:
                            result = self._phase(
                                session, client, context, output, "phase-b", budget
                            )
                        input_count = sum(
                            int(
                                item.get("execution_receipt", {}).get("event_count")
                                or 0
                            )
                            for item in getattr(result, "actions", ())
                            if isinstance(item, dict)
                            and item.get("kind") not in {"done", "fail", "wait"}
                            and item.get("execution_receipt", {}).get("accepted")
                            is True
                        )
                        phase_b.update(
                            _phase_trace(result),
                            executed=input_count > 0,
                            input_event_count=input_count,
                        )
                        policy_incomplete = bool(getattr(result, "incomplete", False))
                    else:
                        phase_b["not_run_reason"] = "decision_budget_exhausted_at_start"
                    after = self._capture(session, output / "after.png", benchmark)
                else:
                    phase_b["not_run_reason"] = "start_failed"
            costs["execution"] = {
                "duration_ms": int((time.monotonic() - execution_started) * 1000),
                "usage": execution_meter.summary(),
            }
            # Raw observation only; final success remains owned by dual-oracle/P2P grading.
            invariant = allowed and phase_b["executed"]
        treatment_tokens = sum(
            int(costs[p]["usage"].get("total_tokens") or 0)
            for p in ("synthesis", "restore", "execution")
        )
        treatment_ms = sum(
            costs[p]["duration_ms"] for p in ("synthesis", "restore", "execution")
        )
        return replace(
            observation,
            after_screenshot=after,
            complete=not policy_incomplete,
            invariants_ok=invariant,
            treatment_tokens=treatment_tokens,
            treatment_time_ms=treatment_ms,
            start_gate_visual=visuals,
            p2p_restore_adapter=self._restorer(context, start_state, checkpoint_sha)
            if start_state is not None
            else None,
        )

    @staticmethod
    def _phase_a_policy_exit(session, phase):
        process = getattr(session, "process", None)
        if process is None or process.poll() != 0:
            return None
        actions = [action for action in getattr(phase, "actions", ()) if isinstance(action, dict) and action.get("execution_receipt", {}).get("accepted") is True]
        if not actions:
            return None
        action = actions[-1]
        if action.get("kind") != "key" or action.get("key") not in {"escape", "esc"} or action.get("modifiers"):
            return None
        return {"phase":"phase-a", "exit_code":0, "target_pid":process.pid,
            "accepted_action":{"kind":"key", "key":"escape", "step":action.get("step"),
                "execution_receipt":action["execution_receipt"]},
            "diagnostic_frame_before_exit":str(getattr(phase, "screenshot", "") or ""),
            "diagnostic_frame_is_terminal_state":False}

    @staticmethod
    def _observe(session):
        try:
            return dict(session.observe_start())
        except RuntimeError as error:
            if isinstance(session, GeneratedJupyterSession) and str(error) == "Jupyter benchmark extension task frame is not open":
                # A policy may close the task panel while the application stays
                # healthy. The sticky launcher flag is not a live Start.
                return {"visible": False, "state": {"configured": False},
                        "failure": "task_workspace_not_open"}
            raise

    @staticmethod
    def _capture(session, path, benchmark, allow_policy=False):
        if benchmark == "jupyterlab":
            try:
                return Path(session.capture_evidence_frame(path))
            except RuntimeError as error:
                if not allow_policy and str(error) != "Jupyter benchmark extension task frame is not open":
                    raise
        return Path(session.capture_policy_frame(path))

    def _restorer(self, context, state, checkpoint_sha):
        # This closure binds the exact state observed at the accepted Start, not a task lookup.
        snapshot = json.loads(json.dumps(state))
        state_sha = canonical_sha256(snapshot)

        def restore(clone, receipt_path, receipt):
            from .lazarus_runtime import _consume_p2p_restore_receipt

            _consume_p2p_restore_receipt(receipt_path, receipt)
            if receipt.get("checkpoint_sha256") != checkpoint_sha:
                raise ValueError("P2P checkpoint binding drift")
            output = clone.parent / (clone.name + "-generated-start-v3")
            ctx = SimpleNamespace(
                key=context.key,
                task=context.task,
                artifact=SimpleNamespace(
                    root=clone,
                    tree_sha256=getattr(context.artifact, "tree_sha256", None),
                    asset_source=str(context.artifact.root),
                ),
            )
            with self._session(
                ctx, benchmark=context.task["benchmark"], output=output
            ) as session:
                session.restore_generated_state(snapshot)
                actual = session.export_state()
                observed = session.observe_start()
                frame = self._capture(
                    session, output / "start.png", context.task["benchmark"]
                )
                applied = actual == snapshot and validate_observed_start(
                    context.task, observed
                )
            return {
                "schema_version": 1,
                "state_restore_applied": applied,
                "adapter": "generated-state-p2p-v3",
                "checkpoint_sha256": checkpoint_sha,
                "checkpoint_kind": receipt["checkpoint_kind"],
                "receipt_sha256": receipt["payload_sha256"],
                "start_frame_sha256": file_sha256(frame),
                "start_state_sha256": state_sha,
                "restored_state_sha256": canonical_sha256(actual),
            }

        return restore

    @contextmanager
    def _session(self, context, *, benchmark, output):
        if benchmark == "jupyterlab":
            asset_identity = canonical_sha256(
                {
                    "source": getattr(
                        context.artifact, "asset_source", str(context.artifact.root)
                    ),
                    "source_sha256": getattr(context.artifact, "tree_sha256", None),
                    "protocol": METHOD_PROTOCOL,
                    "components": file_sha256(
                        Path(__file__).with_name("lazarus_p2p_only_components.js")
                    ),
                    "adapter": file_sha256(
                        Path(__file__).with_name(
                            "lazarus_p2p_only_generated_runtime.py"
                        )
                    ),
                }
            )
            with GeneratedJupyterSession(
                tree=context.artifact.root,
                output_dir=output,
                task_id=context.key.task_id,
                document_cache=self.runtime_root
                / "candidate-assets-v3"
                / asset_identity,
            ) as session:
                yield session
        elif benchmark == "luanti":
            with (
                _NATIVE_INPUT_LOCK,
                GeneratedLuantiSession(
                    tree=context.artifact.root,
                    output_dir=output,
                    task_id=context.key.task_id,
                ) as session,
            ):
                yield session
        elif benchmark == "openra":
            with _NATIVE_INPUT_LOCK:
                prepared = prepare_generated_openra_tree(
                    context.artifact.root, output.parent / "prepared-generated-v3"
                )
                with generated_openra_session(prepared, output) as session:
                    yield session
        else:
            raise ValueError("unsupported benchmark")


def build_p2p_only_paid_runtime_runner(
    workspace, *, runtime_root, provider_timeout_s=900, method_config=None
):
    return P2POnlyPaidRuntimeRunner(
        workspace=Path(workspace),
        runtime_root=Path(runtime_root),
        provider_timeout_s=provider_timeout_s,
        method_config=method_config,
    )
