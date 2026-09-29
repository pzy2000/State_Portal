from __future__ import annotations

import copy
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .c1_evaluation import semantic_assertions, visual_oracle

from .c1_catalog import canonical_benchmark_id
from .reports import new_report_dir, write_json
from .runner import load_spec, run_spec
from .strategies import is_unsupported_reason
from .spacerpg_gui_baseline import run_spacerpg_gui_from_start_baseline
from .oracle_policy import OraclePolicyConfig, oracle_policy_config
from .providers import resolve_model
from .synthesis import DEFAULT_SYNTHESIS_MODEL, SynthesisError, build_synthesis_agent
from .task_schema import (
    CORE_HIDDEN_ORACLES,
    CORE_PUBLIC_TASKS,
    HiddenOracle,
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

VISUAL_ORACLE_PATH_KEYS = ("golden_image", "golden_before", "golden_after", "live_fallback_image")

# Substrings that identify a runtime/environment block (the benchmark cannot execute on
# this machine at all) rather than a model failure. These keep OpenRA-unbuildable /
# missing-desktop-display / uninstalled-engine cases out of the explore-execute failure
# count: such tasks are reported as ``blocked`` (neutral), not ``failure``.
RUNTIME_BLOCKED_MARKERS = (
    "build failed",
    "could not launch",
    "failed to launch",
    "unable to launch",
    "cannot open display",
    "no display",
    "no protocol specified",
    "executable not found",
    "is not installed",
    "command not found",
    "no such file or directory",
    "requires a desktop",
    "requires an interactive",
    "desktop screenshot capture requires",
    "desktop screenshot failed",
    "screencapture",
    "gnome-screenshot",
    "imagemagick import",
    "requires mono",
    "mono is required",
    "libreoffice uno runtime dependencies are not available",
    "libreoffice soffice executable was not found",
    "pyuno",
    "ure_bootstrap",
)


def _runtime_blocked_reason(text: str | None) -> str | None:
    """Return the blocking reason if ``text`` describes a runtime/environment block.

    Reuses the strategy-level unsupported classifier and a small set of launch/build
    signatures. Genuine checkpoint failures (a runtime that ran and failed validation)
    return a report with status ``failure`` instead of raising, so they never reach here.
    """
    if not text:
        return None
    low = text.lower()
    if is_unsupported_reason(low):
        return text
    if any(marker in low for marker in RUNTIME_BLOCKED_MARKERS):
        return text
    return None


def run_core_synthesis_benchmark(
    root: str | Path = Path.cwd(),
    *,
    tasks_path: str | Path = CORE_PUBLIC_TASKS,
    oracle_path: str | Path = CORE_HIDDEN_ORACLES,
    agent_kind: str = "llm",
    model: str = DEFAULT_SYNTHESIS_MODEL,
    base_url: str = "https://api-inference.modelscope.cn/v1",
    api_key_env: str = "MODELSCOPE_API_KEY",
    extra_headers: dict[str, str] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 8192,
    execute: bool = False,
    visual_required: bool = False,
    oracle_policy: str | OraclePolicyConfig | None = None,
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
    headless: bool = True,
    gui_baseline: bool = True,
    explore: bool = True,
    max_explore_steps: int = 12,
    grading_mode: str = "strict",
    report_dir: Path | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    out_dir = report_dir or new_report_dir()
    if grading_mode not in {"strict", "explore_execute"}:
        raise ValueError(f"Unsupported grading_mode: {grading_mode}")
    public_tasks = load_public_issues(base, tasks_path)
    hidden_oracles = load_hidden_oracles(base, oracle_path)
    public_leakage_errors = validate_public_tasks_do_not_reference_hidden(base, tasks_path)
    policy = oracle_policy_config(
        oracle_policy=oracle_policy,
        visual_required=visual_required,
        visual_sample_rate=visual_sample_rate,
        visual_on_semantic_failure=visual_on_semantic_failure,
    )
    if grading_mode == "explore_execute":
        # The combined grade needs an executed checkpoint plus a visual verdict, so
        # force execution on and, by default, upgrade optional policies to one that
        # always produces a visual verdict. Keep fake-visual available for offline
        # plumbing checks. An *explicit* ``semantic`` request is honoured as a
        # deliberate semantic-only run: the visual audit is skipped and the grade
        # collapses to target + execution (``graded_on: "execution"``). This is the
        # supported path for runtimes whose committed goldens cannot be reproduced on
        # this host (e.g. the luanti voxel frames that need a desktop GL session).
        execute = True
        requested_policy: str | None = None
        if isinstance(oracle_policy, OraclePolicyConfig):
            requested_policy = oracle_policy.policy
        elif isinstance(oracle_policy, str):
            requested_policy = oracle_policy
        if requested_policy != "semantic" and policy.policy not in {"visual", "fake-visual"}:
            policy = oracle_policy_config(
                oracle_policy="visual",
                visual_sample_rate=visual_sample_rate,
                visual_on_semantic_failure=visual_on_semantic_failure,
            )
    cascade = cascade_config or cascade_policy_config()
    effective_model = model
    effective_base_url = base_url
    effective_api_key_env = api_key_env
    effective_headers = extra_headers
    if agent_kind == "llm":
        # Route ``model[@provider]`` to its gateway; untagged ids keep the
        # run-level default provider (incl. ModelScope ``:DashScope`` routing).
        effective_model, provider = resolve_model(
            model, default_base_url=base_url, default_api_key_env=api_key_env
        )
        effective_base_url = provider.base_url
        effective_api_key_env = provider.api_key_env
        if extra_headers is None:
            effective_headers = provider.extra_headers
        if not os.environ.get(effective_api_key_env):
            raise SynthesisError(f"{effective_api_key_env} is required for core-bench --agent llm.")
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

    results: list[dict[str, Any]] = []
    total_tasks = len(public_tasks)
    label_model = effective_model if agent_kind == "llm" else agent_kind
    print(
        f"[core-synth] {label_model}: {total_tasks} tasks, agent={agent_label}, explore={explore}",
        flush=True,
    )
    for position, task in enumerate(public_tasks, start=1):
        task_start = time.perf_counter()
        print(
            f"[core-synth] {label_model} ({position}/{total_tasks}) "
            f"{task.benchmark}/{task.id}: synthesizing...",
            flush=True,
        )
        result: dict[str, Any] = {
            "task_id": task.id,
            "benchmark": task.benchmark,
            "public_issue": task.public_payload(),
            "public_issue_path": task.source_path,
            "execute": execute,
            "visual_required": policy.policy != "semantic",
            "oracle_policy": policy.policy,
            "synthesis_agent": agent_label,
        }
        try:
            checkpoint = agent.synthesize(task, base, artifact_dir=out_dir / "synthesis-artifacts" / task.id)
            oracle = hidden_oracles.get(task.id)
            if oracle is None:
                result["grading"] = {"status": "failure", "reason": "missing hidden oracle"}
                result["grading_with_scaffold"] = {"status": "failure", "reason": "missing hidden oracle"}
                hidden_spec: dict[str, Any] | None = None
                hidden_spec_path = None
            else:
                hidden_spec_path = resolve_hidden_spec(base, oracle)
                hidden_spec = load_spec(hidden_spec_path)
                result["hidden_oracle_path"] = oracle.source_path
            checkpoint_dict, runtime_report = _maybe_execute_checkpoint(
                base,
                task,
                checkpoint,
                out_dir,
                execute=execute,
                headless=headless,
                oracle_policy=policy,
                cascade_config=cascade,
                hidden_spec=hidden_spec,
                hidden_spec_path=hidden_spec_path,
            )
            if hidden_spec is not None:
                if grading_mode == "explore_execute":
                    # Headline grading is combined: target intent + real execution +
                    # visual oracle. The strict structural grade is kept as a
                    # secondary signal so the transcription ablation stays visible.
                    result["grading"] = grade_method_checkpoint_explore(checkpoint_dict, hidden_spec, base)
                    result["grading_structural"] = grade_method_checkpoint_strict(checkpoint_dict, hidden_spec, base)
                else:
                    # Headline grading is strict: it grades the method's own proposed spec.
                    result["grading"] = grade_method_checkpoint_strict(checkpoint_dict, hidden_spec, base)
                # Ablation grading reuses the prior lenient path that grades the
                # canonicalized executable spec (the public-task scaffold).
                result["grading_with_scaffold"] = grade_method_checkpoint(checkpoint_dict, hidden_spec, base)
            result["method_checkpoint"] = checkpoint_dict
            result["runtime_report"] = runtime_report
            gui_baseline_result = gui_from_start_baseline(
                task,
                hidden_spec,
                execute=execute,
                root=base,
                report_dir=out_dir / "gui-from-start" / task.id,
                headless=headless,
                enabled=gui_baseline,
            )
            result["baselines"] = {
                "gui_from_start": gui_baseline_result,
                "scripted_replay": scripted_replay_baseline(task, hidden_spec),
            }
            result["schema_errors"] = validate_method_checkpoint(checkpoint_dict, base)
        except SynthesisError as error:
            result["grading"] = {"status": "failure", "reason": str(error)}
            result["synthesis"] = {"status": "failure", "error": str(error), **error.artifacts}
            result["schema_errors"] = error.artifacts.get("schema_errors", [str(error)])
        except Exception as error:
            result["grading"] = {"status": "failure", "reason": str(error)}
            result["schema_errors"] = [str(error)]
        results.append(result)
        elapsed = time.perf_counter() - task_start
        strict_status = (result.get("grading") or {}).get("status", "?")
        scaffold_status = (result.get("grading_with_scaffold") or {}).get("status", "-")
        checkpoint_dict_log = result.get("method_checkpoint")
        evidence = (
            checkpoint_dict_log.get("source_evidence")
            if isinstance(checkpoint_dict_log, dict)
            else None
        )
        n_evidence = len(evidence) if isinstance(evidence, list) else 0
        print(
            f"[core-synth] {label_model} ({position}/{total_tasks}) "
            f"{task.id}: strict={strict_status} scaffold={scaffold_status} "
            f"evidence={n_evidence} ({elapsed:.1f}s)",
            flush=True,
        )

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "experiment": "core_synthesis",
        "grading_mode": grading_mode,
        "synthesis_agent": agent_label,
        "synthesis_model": effective_model if agent_kind == "llm" else None,
        "exploration": explore,
        "max_explore_steps": max_explore_steps if explore else None,
        "visual_required": policy.policy != "semantic",
        "oracle_policy": policy.policy,
        "visual_sample_rate": policy.visual_sample_rate,
        "visual_on_semantic_failure": policy.visual_on_semantic_failure,
        "claim_boundary": "Public issue and source code are method inputs; hidden target/oracle is used only by the grader.",
        "public_task_file": str((base / tasks_path).resolve() if not Path(tasks_path).is_absolute() else tasks_path),
        "hidden_oracle_file": str((base / oracle_path).resolve() if not Path(oracle_path).is_absolute() else oracle_path),
        "public_leakage_errors": public_leakage_errors,
        "results": results,
        "summary": summarize_core_results(results, public_leakage_errors),
    }
    write_json(out_dir / "core-synthesis.json", payload)
    (out_dir / "core-synthesis.md").write_text(render_core_synthesis_markdown(payload), encoding="utf-8")
    return {**payload, "report_dir": str(out_dir)}


def run_core_multimodel_benchmark(
    root: str | Path = Path.cwd(),
    *,
    models: list[str],
    tasks_path: str | Path = CORE_PUBLIC_TASKS,
    oracle_path: str | Path = CORE_HIDDEN_ORACLES,
    base_url: str = "https://api-inference.modelscope.cn/v1",
    api_key_env: str = "MODELSCOPE_API_KEY",
    temperature: float = 0.2,
    max_tokens: int = 8192,
    explore: bool = True,
    max_explore_steps: int = 12,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the strict core synthesis protocol across several LLMs and build a leaderboard.

    Each model is graded with the strict oracle (its own proposed checkpoint) and with
    the lenient scaffold oracle (the prior pipeline). Reporting both is the scaffold
    ablation: it isolates the model contribution from the public-task scaffold.
    """

    base = Path(root).expanduser().resolve()
    out_dir = report_dir or new_report_dir()

    # Resolve every model's provider up front so the run fails fast if any
    # required API key is missing, with a single message naming all of them.
    resolved = [
        (model, *resolve_model(model, default_base_url=base_url, default_api_key_env=api_key_env))
        for model in models
    ]
    missing_keys = sorted({prov.api_key_env for _, _, prov in resolved if not os.environ.get(prov.api_key_env)})
    if missing_keys:
        raise SynthesisError(
            "Missing API key environment variable(s) for the multi-model core benchmark: "
            + ", ".join(missing_keys)
        )

    leaderboard: list[dict[str, Any]] = []
    per_task: list[dict[str, Any]] = []
    print(f"[core-multimodel] report_dir={out_dir} models={len(models)}", flush=True)
    for index, (model, _clean_model, provider) in enumerate(resolved, start=1):
        print(f"[core-multimodel] ({index}/{len(models)}) running {model} via {provider.name}", flush=True)
        model_dir = out_dir / "models" / _model_slug(model)
        run = run_core_synthesis_benchmark(
            base,
            tasks_path=tasks_path,
            oracle_path=oracle_path,
            agent_kind="llm",
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            temperature=temperature,
            max_tokens=max_tokens,
            execute=False,
            gui_baseline=False,
            explore=explore,
            max_explore_steps=max_explore_steps,
            report_dir=model_dir,
        )
        summary = run.get("summary", {})
        total = summary.get("total_tasks", 0)
        strict_success = summary.get("method_checkpoint_success", 0)
        scaffold_success = summary.get("scaffold_graded_success", 0)
        synthesis_failures = summary.get("synthesis_failures", 0)
        by_benchmark: dict[str, dict[str, int]] = {}
        for item in run.get("results", []):
            bench = str(item.get("benchmark"))
            bucket = by_benchmark.setdefault(bench, {"total": 0, "strict_success": 0})
            bucket["total"] += 1
            if item.get("grading", {}).get("status") == "success":
                bucket["strict_success"] += 1
            per_task.append(
                {
                    "model": model,
                    "task_id": item.get("task_id"),
                    "benchmark": bench,
                    "strict_status": item.get("grading", {}).get("status"),
                    "scaffold_status": item.get("grading_with_scaffold", {}).get("status"),
                    "used_public_scaffold": (item.get("method_checkpoint") or {}).get("used_public_scaffold"),
                }
            )
        leaderboard.append(
            {
                "model": model,
                "provider": provider.name,
                "base_url": provider.base_url,
                "tasks": total,
                "strict_success": strict_success,
                "scaffold_success": scaffold_success,
                "synthesis_failures": synthesis_failures,
                "public_leakage_failures": summary.get("public_leakage_failures", 0),
                "by_benchmark": by_benchmark,
                "report_dir": run.get("report_dir"),
            }
        )
        print(
            f"[core-multimodel] {model}: strict={strict_success}/{total} "
            f"scaffold={scaffold_success}/{total} synth_fail={synthesis_failures}",
            flush=True,
        )

    leaderboard.sort(key=lambda row: (-row["strict_success"], row["model"]))
    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "experiment": "core_synthesis_multimodel",
        "base_url": base_url,
        "models": models,
        "exploration": explore,
        "max_explore_steps": max_explore_steps if explore else None,
        "claim_boundary": "Strict grading evaluates each model's own checkpoint; scaffold grading is reported only for the ablation.",
        "leaderboard": leaderboard,
        "per_task": per_task,
    }
    write_json(out_dir / "core-multimodel.json", payload)
    (out_dir / "core-multimodel.md").write_text(render_multimodel_markdown(payload), encoding="utf-8")
    return {**payload, "report_dir": str(out_dir)}


def render_multimodel_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Core Synthesis Multi-Model Leaderboard",
        "",
        f"- Generated: `{payload.get('generated_at_utc')}`",
        f"- Default base URL: `{payload.get('base_url')}`",
        f"- Claim boundary: {payload.get('claim_boundary')}",
        "",
        "| Model | Provider | Strict success | Scaffold success | Synthesis failures |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in payload.get("leaderboard", []):
        total = row.get("tasks", 0)
        lines.append(
            "| "
            + " | ".join(
                [
                    str(row.get("model")),
                    str(row.get("provider", "")),
                    f"{row.get('strict_success', 0)}/{total}",
                    f"{row.get('scaffold_success', 0)}/{total}",
                    str(row.get("synthesis_failures", 0)),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _model_slug(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "__", model).strip("_")


def grade_method_checkpoint(checkpoint: MethodCheckpoint | dict[str, Any], hidden_spec: dict[str, Any], root: str | Path = Path.cwd()) -> dict[str, Any]:
    data = checkpoint.to_dict() if isinstance(checkpoint, MethodCheckpoint) else checkpoint
    generated_spec = data.get("generated_spec") if isinstance(data.get("generated_spec"), dict) else {}
    schema_errors = validate_method_checkpoint(data, root)
    target_match = _normalized(generated_spec.get("target")) == _normalized(hidden_spec.get("target"))
    action_match = _normalized(generated_spec.get("actions")) == _normalized(hidden_spec.get("actions"))
    hidden_assertions_covered = _list_contains_all(
        generated_spec.get("assertions", []), semantic_assertions(hidden_spec) or []
    )
    strategy_match = _normalized(generated_spec.get("checkpoint")) == _normalized(hidden_spec.get("checkpoint"))
    benchmark_match = _benchmark_matches(
        generated_spec.get("benchmark"),
        hidden_spec.get("benchmark"),
    )
    status = "success" if benchmark_match and target_match and action_match and hidden_assertions_covered and strategy_match and not schema_errors else "failure"
    return {
        "status": status,
        "benchmark_match": benchmark_match,
        "target_match": target_match,
        "action_match": action_match,
        "hidden_assertions_covered": hidden_assertions_covered,
        "strategy_match": strategy_match,
        "hidden_oracle_used_only_by_grader": True,
        "schema_errors": schema_errors,
    }


def grade_method_checkpoint_strict(
    checkpoint: MethodCheckpoint | dict[str, Any],
    hidden_spec: dict[str, Any],
    root: str | Path = Path.cwd(),
) -> dict[str, Any]:
    """Strict oracle that grades the method's own proposed spec, not the scaffold.

    The hidden spec must be covered by the method output: the method has to supply
    every semantic target field, every required action, every hidden assertion, and
    the declared checkpoint strategy. Extra method fields are tolerated, but missing
    or contradicting deep-state values fail the task. This exposes whether a model
    actually recovered the deep target rather than relying on a public-task scaffold.
    """

    data = checkpoint.to_dict() if isinstance(checkpoint, MethodCheckpoint) else checkpoint
    schema_errors = validate_method_checkpoint(data, root)
    model_spec = data.get("model_generated_spec")
    if not isinstance(model_spec, dict):
        return {
            "status": "failure",
            "graded_on": "model_generated_spec",
            "reason": "method did not return an executable spec mapping",
            "benchmark_match": False,
            "target_match": False,
            "action_match": False,
            "hidden_assertions_covered": False,
            "strategy_match": False,
            "hidden_oracle_used_only_by_grader": True,
            "schema_errors": schema_errors,
        }
    benchmark_match = _benchmark_matches(model_spec.get("benchmark"), hidden_spec.get("benchmark"))
    target_match = _target_covered(model_spec, hidden_spec.get("target"))
    action_match = _actions_covered(model_spec.get("actions"), hidden_spec.get("actions"))
    hidden_assertions_covered = _covers_list(
        model_spec.get("assertions"), semantic_assertions(hidden_spec)
    )
    strategy_match = _strategy_covered(model_spec.get("checkpoint"), hidden_spec.get("checkpoint"))
    # Success requires recovering the deep semantic checkpoint: the start-state
    # target, the checkpoint strategy, and the smoke actions that drive it. Exact
    # reproduction of the grader's assertion values (often hidden post-state numbers
    # the issue never reveals) is reported as a stricter secondary signal, not a
    # success requirement, so the score reflects checkpoint synthesis rather than
    # guessing private oracle thresholds.
    status = (
        "success"
        if benchmark_match
        and target_match
        and action_match
        and strategy_match
        and not schema_errors
        else "failure"
    )
    return {
        "status": status,
        "graded_on": "model_generated_spec",
        "benchmark_match": benchmark_match,
        "target_match": target_match,
        "action_match": action_match,
        "hidden_assertions_covered": hidden_assertions_covered,
        "strategy_match": strategy_match,
        "hidden_oracle_used_only_by_grader": True,
        "schema_errors": schema_errors,
    }


def grade_method_checkpoint_explore(
    checkpoint: MethodCheckpoint | dict[str, Any],
    hidden_spec: dict[str, Any],
    root: str | Path = Path.cwd(),
) -> dict[str, Any]:
    """Combined oracle for the explore-hard variant.

    Success requires three things at once: the model recovered the semantic target
    intent, the checkpoint executed successfully against the real runtime, and the
    hidden visual oracle passed. The structural action/strategy matches are reported
    as secondary signals only, because the de-leaked issue deliberately withholds the
    strategy/actions/hook names so the model must discover them from source rather
    than transcribe them. This shifts the headline signal from structural transcription
    to executable, visually-verified checkpoint synthesis.

    The runtime status fields are populated by ``_maybe_execute_checkpoint`` when the
    benchmark runs with execution + a non-semantic oracle policy; without them the
    explore grade fails (execution is a hard requirement here).
    """

    data = checkpoint.to_dict() if isinstance(checkpoint, MethodCheckpoint) else checkpoint
    structural = grade_method_checkpoint_strict(data, hidden_spec, root)
    benchmark_match = bool(structural.get("benchmark_match"))
    target_match = bool(structural.get("target_match"))

    semantic_status = data.get("runtime_semantic_status")
    overall_status = data.get("runtime_overall_status")
    runtime_visual = data.get("runtime_visual_validation") if isinstance(data.get("runtime_visual_validation"), dict) else {}
    runtime_validation = data.get("runtime_validation") if isinstance(data.get("runtime_validation"), dict) else {}

    # Blocked: the runtime/environment could not execute this benchmark on this
    # machine at all (engine unbuildable, missing desktop display, uninstalled
    # binary). This is independent of the model's answer, so it is reported as a
    # neutral ``blocked`` status rather than a failure. The structural signal is
    # still recorded so the transcription ablation stays visible.
    runtime_blocked = data.get("runtime_blocked") if isinstance(data.get("runtime_blocked"), dict) else None
    blocked_reason = None
    if runtime_blocked:
        blocked_reason = runtime_blocked.get("reason")
    elif runtime_validation.get("status") == "unsupported":
        blocked_reason = runtime_validation.get("detail") or "runtime reported unsupported"
    if blocked_reason:
        return {
            "status": "blocked",
            "graded_on": "execution+visual",
            "benchmark_match": benchmark_match,
            "target_match": target_match,
            "execution_passed": False,
            "visual_passed": False,
            "blocked_reason": blocked_reason,
            "execution_status": "blocked",
            "visual_status": runtime_visual.get("status"),
            "structural_signal": structural,
            "hidden_oracle_used_only_by_grader": True,
            "schema_errors": structural.get("schema_errors", []),
        }

    # Execution counts as passing when the semantic run reached and validated the
    # target and its smoke oracle succeeded. Fall back to overall_status if the
    # semantic field is absent (older execution paths).
    execution_passed = (semantic_status or overall_status) == "success"
    visual_status = runtime_visual.get("status")

    # Visual is graded semantic-only when no real verdict was produced (the visual
    # audit was skipped by the oracle policy). This is the intended path for runtimes
    # whose committed goldens are unreliable on this host (e.g. the luanti voxel
    # frames, which need an unlocked desktop GL session to regenerate): run that
    # subset with ``--oracle-policy semantic`` and the grade collapses to
    # target + execution instead of penalising the missing/placeholder frame. When a
    # real visual verdict *is* present it stays a hard requirement.
    visual_graded = visual_status not in (None, "skipped")
    visual_passed = visual_status == "success"
    if visual_graded:
        graded_on = "execution+visual"
        success = benchmark_match and target_match and execution_passed and visual_passed
    else:
        graded_on = "execution"
        success = benchmark_match and target_match and execution_passed

    status = "success" if success else "failure"
    return {
        "status": status,
        "graded_on": graded_on,
        "benchmark_match": benchmark_match,
        "target_match": target_match,
        "execution_passed": execution_passed,
        "visual_passed": visual_passed,
        "visual_graded": visual_graded,
        "execution_status": semantic_status or overall_status,
        "visual_status": visual_status,
        "structural_signal": structural,
        "hidden_oracle_used_only_by_grader": True,
        "schema_errors": structural.get("schema_errors", []),
    }


def gui_from_start_baseline(
    task: PublicIssue,
    hidden_spec: dict[str, Any] | None,
    *,
    execute: bool = False,
    root: str | Path = Path.cwd(),
    report_dir: Path | None = None,
    headless: bool = True,
    enabled: bool = True,
) -> dict[str, Any]:
    if not enabled:
        return {
            "status": "skipped",
            "public_inputs_only": True,
            "strict_no_checkpoint": True,
            "reason": "GUI-from-start baseline was disabled for this run.",
        }
    if task.benchmark == "SpaceRPG":
        return run_spacerpg_gui_from_start_baseline(
            task,
            hidden_spec,
            root=root,
            report_dir=report_dir,
            headless=headless,
        )
    if task.benchmark == "2048":
        return {
            "status": "attempted_not_executed" if not execute else "attempted",
            "public_inputs_only": True,
            "strict_no_checkpoint": True,
            "budget_ms": task.budget_ms,
            "actions_attempted": ["launch_app", "use_gui_or_keyboard_from_clean_start"],
            "reason": "GUI-from-start baseline does not receive checkpoint payloads or hidden target/oracle.",
        }
    return {
        "status": "attempted_not_executed" if not execute else "attempted",
        "public_inputs_only": True,
        "strict_no_checkpoint": True,
        "budget_ms": task.budget_ms,
        "actions_attempted": ["launch_app", "complete_setup_from_public_issue", "navigate_with_gui_controls"],
        "reason": "GUI-from-start baseline receives the same public issue and no hidden oracle.",
    }


def scripted_replay_baseline(task: PublicIssue, hidden_spec: dict[str, Any] | None) -> dict[str, Any]:
    if not hidden_spec or "replay_from_start" not in hidden_spec:
        return {"status": "unsupported", "reason": "No deterministic replay_from_start plan is declared for this task."}
    return {
        "status": "declared",
        "paper_role": "scripted_replay_upper_bound",
        "uses_hidden_replay_plan": True,
        "reason": "Declared replay baselines are reported separately from public-issue-only GUI baselines.",
        "actions": hidden_spec.get("replay_from_start", {}).get("actions", []),
    }


def summarize_core_results(results: list[dict[str, Any]], public_leakage_errors: list[str] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total_tasks": len(results),
        "public_leakage_failures": len(public_leakage_errors or []),
        "method_checkpoint_success": 0,
        "method_checkpoint_failure": 0,
        "explore_execute_success": 0,
        "explore_execute_blocked": 0,
        "explore_target_and_execution": 0,
        "scaffold_graded_success": 0,
        "used_public_scaffold": 0,
        "schema_error_tasks": 0,
        "hidden_oracle_grader_only": 0,
        "validity_classes": {},
        "payload_kinds": {},
        "runtime_validation_status": {},
        "runtime_visual_status": {},
        "synthesis_failures": 0,
        "gui_from_start": {"total": 0, "success": 0, "timeout_or_not_reached": 0, "public_inputs_only": 0, "with_action_trace": 0},
    }
    for result in results:
        grading = result.get("grading") if isinstance(result.get("grading"), dict) else {}
        grading_status = grading.get("status")
        if grading_status == "success":
            summary["method_checkpoint_success"] += 1
        elif grading_status == "blocked":
            # Neutral: the runtime could not execute here; not a model failure.
            pass
        else:
            summary["method_checkpoint_failure"] += 1
        if grading.get("graded_on") in ("execution+visual", "execution"):
            if grading_status == "success":
                summary["explore_execute_success"] += 1
            elif grading_status == "blocked":
                summary["explore_execute_blocked"] += 1
            if grading.get("target_match") and grading.get("execution_passed"):
                summary["explore_target_and_execution"] += 1
        scaffold_grading = result.get("grading_with_scaffold") if isinstance(result.get("grading_with_scaffold"), dict) else {}
        if scaffold_grading.get("status") == "success":
            summary["scaffold_graded_success"] += 1
        checkpoint_for_scaffold = result.get("method_checkpoint") if isinstance(result.get("method_checkpoint"), dict) else {}
        if checkpoint_for_scaffold.get("used_public_scaffold"):
            summary["used_public_scaffold"] += 1
        if result.get("schema_errors"):
            summary["schema_error_tasks"] += 1
        if result.get("synthesis", {}).get("status") == "failure":
            summary["synthesis_failures"] += 1
        if grading.get("hidden_oracle_used_only_by_grader"):
            summary["hidden_oracle_grader_only"] += 1
        checkpoint = result.get("method_checkpoint") if isinstance(result.get("method_checkpoint"), dict) else {}
        _count(summary["validity_classes"], checkpoint.get("validity_class", "unknown"))
        payload = checkpoint.get("payload") if isinstance(checkpoint.get("payload"), dict) else {}
        _count(summary["payload_kinds"], payload.get("kind", "unknown"))
        runtime = checkpoint.get("runtime_validation") if isinstance(checkpoint.get("runtime_validation"), dict) else {}
        _count(summary["runtime_validation_status"], runtime.get("status", "unknown"))
        runtime_visual = checkpoint.get("runtime_visual_validation") if isinstance(checkpoint.get("runtime_visual_validation"), dict) else {}
        _count(summary["runtime_visual_status"], runtime_visual.get("status", "unknown"))
        baseline = result.get("baselines", {}).get("gui_from_start", {}) if isinstance(result.get("baselines"), dict) else {}
        summary["gui_from_start"]["total"] += 1
        if baseline.get("status") == "success":
            summary["gui_from_start"]["success"] += 1
        if baseline.get("status") in {"timeout", "not_reached"}:
            summary["gui_from_start"]["timeout_or_not_reached"] += 1
        if baseline.get("public_inputs_only"):
            summary["gui_from_start"]["public_inputs_only"] += 1
        if baseline.get("action_trace"):
            summary["gui_from_start"]["with_action_trace"] += 1
    return summary


def render_core_synthesis_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# Core Synthesis Benchmark",
        "",
        f"- Generated: `{payload.get('generated_at_utc')}`",
        f"- Synthesis agent: `{payload.get('synthesis_agent')}`",
        f"- Synthesis model: `{payload.get('synthesis_model') or ''}`",
        f"- Tasks: `{summary.get('total_tasks', 0)}`",
        f"- Method checkpoint success: `{summary.get('method_checkpoint_success', 0)}`",
        f"- Explore-execute success: `{summary.get('explore_execute_success', 0)}`",
        f"- Explore-execute blocked (runtime unavailable): `{summary.get('explore_execute_blocked', 0)}`",
        f"- Public leakage failures: `{summary.get('public_leakage_failures', 0)}`",
        f"- Claim boundary: {payload.get('claim_boundary')}",
        "",
        "## Results",
        "| task | benchmark | grade | validity | payload | runtime_validation | runtime_visual | gui_from_start |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in payload.get("results", []):
        checkpoint = result.get("method_checkpoint", {}) if isinstance(result.get("method_checkpoint"), dict) else {}
        payload_data = checkpoint.get("payload", {}) if isinstance(checkpoint.get("payload"), dict) else {}
        runtime = checkpoint.get("runtime_validation", {}) if isinstance(checkpoint.get("runtime_validation"), dict) else {}
        runtime_visual = checkpoint.get("runtime_visual_validation", {}) if isinstance(checkpoint.get("runtime_visual_validation"), dict) else {}
        gui = result.get("baselines", {}).get("gui_from_start", {}) if isinstance(result.get("baselines"), dict) else {}
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result.get("task_id")),
                    str(result.get("benchmark")),
                    str(result.get("grading", {}).get("status")),
                    str(checkpoint.get("validity_class")),
                    str(payload_data.get("kind")),
                    str(runtime.get("status")),
                    str(runtime_visual.get("status")),
                    str(gui.get("status")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _maybe_execute_checkpoint(
    root: Path,
    task: PublicIssue,
    checkpoint: MethodCheckpoint,
    out_dir: Path,
    *,
    execute: bool,
    headless: bool,
    oracle_policy: OraclePolicyConfig | None = None,
    cascade_config: CascadePolicyConfig | None = None,
    hidden_spec: dict[str, Any] | None = None,
    hidden_spec_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    checkpoint_dict = checkpoint.to_dict()
    if not execute:
        return checkpoint_dict, None
    policy = oracle_policy or OraclePolicyConfig()
    spec_path = out_dir / "method-specs" / f"{task.id}.yaml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    executable_spec = copy.deepcopy(checkpoint.generated_spec)
    if policy.policy != "semantic":
        _attach_hidden_visual_oracle(executable_spec, hidden_spec, hidden_spec_path)
    spec_path.write_text(yaml.safe_dump(executable_spec, sort_keys=False, allow_unicode=False), encoding="utf-8")
    try:
        report = run_spec(
            spec_path,
            repo_arg=str(resolve_task_repo(root, task)),
            report_dir=out_dir / "method-runs" / task.id,
            headless=headless,
            oracle_policy=policy.policy,
            visual_sample_rate=policy.visual_sample_rate,
            visual_on_semantic_failure=policy.visual_on_semantic_failure,
            cascade_config=cascade_config,
        )
    except Exception as error:  # noqa: BLE001 - classify runtime blocks vs real failures
        blocked_reason = _runtime_blocked_reason(str(error))
        if blocked_reason is None:
            raise
        # The runtime/environment cannot execute this benchmark here (e.g. OpenRA
        # cannot build without mono, or an engine needs an unlocked desktop GUI
        # session). Record it as blocked so the explore grade does not count it as a
        # model failure; re-raise anything that looks like a genuine failure.
        checkpoint_dict["runtime_blocked"] = {
            "reason": blocked_reason,
            "error_type": type(error).__name__,
        }
        return checkpoint_dict, None
    checkpoint_dict["runtime_validation"] = asdict(report.validation)
    checkpoint_dict["runtime_visual_validation"] = asdict(report.visual_oracle_result)
    checkpoint_dict["runtime_overall_status"] = report.overall_status
    checkpoint_dict["runtime_semantic_status"] = report.semantic_status
    return checkpoint_dict, report.to_dict()


def _attach_hidden_visual_oracle(
    executable_spec: dict[str, Any],
    hidden_spec: dict[str, Any] | None,
    hidden_spec_path: Path | None,
) -> None:
    oracle_value = visual_oracle(hidden_spec) if hidden_spec is not None else None
    if not isinstance(oracle_value, dict):
        raise SynthesisError("core-bench --visual requires the hidden spec to declare visual_oracle.")
    oracle = copy.deepcopy(oracle_value)
    if hidden_spec_path is not None and isinstance(oracle, dict):
        _make_visual_oracle_paths_absolute(oracle, hidden_spec_path.parent)
    executable_spec["visual_oracle"] = oracle


def _make_visual_oracle_paths_absolute(oracle: dict[str, Any], base_dir: Path) -> None:
    for key in VISUAL_ORACLE_PATH_KEYS:
        value = oracle.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        path = Path(value).expanduser()
        oracle[key] = str(path if path.is_absolute() else (base_dir / path).resolve())


def _normalized(value: Any) -> Any:
    return copy.deepcopy(value)


def _benchmark_matches(actual: Any, expected: Any) -> bool:
    try:
        return canonical_benchmark_id(actual) == canonical_benchmark_id(expected)
    except ValueError:
        return str(actual) == str(expected)


_TIMING_KEYS = {"wait_ms", "ms", "timeout_ms", "delay_ms"}


def _scalar_token(value: Any) -> str:
    """Normalize a scalar so representational variants compare equal.

    Casing and the separators used in identifiers vary across models (for example
    FLIGHT vs flight, helion-reach vs helion_reach), but they denote the same
    semantic value. Integral floats are folded to integers.
    """

    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        number = float(value)
        return str(int(number)) if number.is_integer() else str(number)
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _scalar_equal(actual: Any, expected: Any) -> bool:
    if actual is _MISSING:
        return False
    return _scalar_token(actual) == _scalar_token(expected)


def _covers(actual: Any, expected: Any) -> bool:
    """True when ``actual`` contains every value required by ``expected`` (deep)."""

    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return all(_covers(actual.get(key, _MISSING), value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(_covers(actual[index], value) for index, value in enumerate(expected))
    if actual is _MISSING:
        return False
    return _scalar_equal(actual, expected)


def _covers_list(actual: Any, expected: Any) -> bool:
    """True when every expected item is covered by some item in ``actual``."""

    if not isinstance(expected, list):
        return False
    if not expected:
        return True
    if not isinstance(actual, list):
        return False
    return all(any(_covers(candidate, item) for candidate in actual) for item in expected)


def _collect_scalar_tokens(value: Any, out: set[str]) -> None:
    if isinstance(value, dict):
        for nested in value.values():
            _collect_scalar_tokens(nested, out)
    elif isinstance(value, list):
        for nested in value:
            _collect_scalar_tokens(nested, out)
    elif value is not None:
        out.add(_scalar_token(value))


def _target_covered(model_spec: dict[str, Any], hidden_target: Any) -> bool:
    """Check the method recovered the hidden semantic target.

    Nested structures such as boards keep exact (separator/case-insensitive)
    structural equality. Scalar target fields are satisfied when the value appears
    anywhere in the method's target or smoke actions, because models legitimately
    encode a target field inside the action payload that drives the checkpoint.
    """

    if not isinstance(hidden_target, dict):
        return _covers(model_spec.get("target"), hidden_target)
    model_target = model_spec.get("target") if isinstance(model_spec.get("target"), dict) else {}
    tokens: set[str] = set()
    _collect_scalar_tokens(model_target, tokens)
    _collect_scalar_tokens(model_spec.get("actions"), tokens)
    for key, value in hidden_target.items():
        if isinstance(value, (dict, list)):
            if not _covers(model_target.get(key, _MISSING), value):
                return False
        elif _scalar_token(value) not in tokens:
            return False
    return True


def _actions_covered(model_actions: Any, hidden_actions: Any) -> bool:
    """Each hidden action's semantic tokens must appear in the method's actions.

    Incidental timing knobs are dropped so the comparison reflects the operation
    (key press, hook method, arguments) rather than wait durations.
    """

    if not isinstance(hidden_actions, list):
        return False
    if not hidden_actions:
        return True
    model_tokens: set[str] = set()
    for action in (model_actions or []):
        _collect_scalar_tokens(_strip_timing(action), model_tokens)
    for hidden_action in hidden_actions:
        hidden_tokens: set[str] = set()
        _collect_scalar_tokens(_strip_timing(hidden_action), hidden_tokens)
        if not hidden_tokens.issubset(model_tokens):
            return False
    return True


def _strip_timing(action: Any) -> Any:
    if isinstance(action, dict):
        return {key: value for key, value in action.items() if key not in _TIMING_KEYS}
    return action


def _strategy_covered(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, dict):
        return True
    if not isinstance(actual, dict):
        return False
    expected_strategies = expected.get("allowed_strategies") or []
    actual_strategies = actual.get("allowed_strategies") or []
    if isinstance(expected_strategies, list) and isinstance(actual_strategies, list):
        if not all(strategy in actual_strategies for strategy in expected_strategies):
            return False
    elif expected_strategies != actual_strategies:
        return False
    if "expected_validity" in expected:
        return actual.get("expected_validity") == expected.get("expected_validity")
    return True


_MISSING = object()


def _list_contains_all(actual: Any, expected: Any) -> bool:
    if not isinstance(actual, list) or not isinstance(expected, list):
        return False
    actual_normalized = [_normalized(item) for item in actual]
    return all(_normalized(item) in actual_normalized for item in expected)


def _count(bucket: dict[str, int], key: Any) -> None:
    name = str(key)
    bucket[name] = bucket.get(name, 0) + 1
