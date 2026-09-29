from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Callable

import yaml

from .blender_runtime import BlenderRuntimeAdapter
from .c1_evaluation.contract import materialize_legacy_spec
from .discovery import RepositoryInspector
from .jupyterlab_runtime import JupyterLabRuntimeAdapter
from .libreoffice_runtime import LibreOfficeRuntimeAdapter
from .models import RunReport
from .jynew_runtime import JynewRuntimeAdapter, ensure_jynew_instrumentation
from .luanti_runtime import LuantiRuntimeAdapter, ensure_luanti_instrumentation
from .oracle_policy import apply_oracle_policy, oracle_policy_config, policy_may_capture_visual
from .openra_runtime import OpenRARuntimeAdapter
from .pal3_runtime import Pal3RuntimeAdapter, ensure_pal3_instrumentation
from .profiles import profile_for
from .reports import new_report_dir, write_json, write_run_report
from .runtime import BrowserRuntimeAdapter
from .runtime_lock import runtime_lease
from .spec_schema import ensure_valid_spec_schema
from .strategies import is_unsupported_reason
from .visual import CascadePolicyConfig, cascade_policy_config


ABLATION_MODES = ["inject_or_restore_no_validation", "jump_and_validate", "replay_from_start"]
SMOKE_SPEC_KEYS = {
    "name",
    "benchmark",
    "target",
    "checkpoint",
    "actions",
    "assertions",
    "visual_oracle",
    "evaluation",
    "replay_from_start",
}


def load_raw_spec(spec_path: str | Path) -> dict[str, Any]:
    path = Path(spec_path).expanduser().resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Spec must be a YAML mapping: {path}")
    if any(key in data for key in SMOKE_SPEC_KEYS):
        ensure_valid_spec_schema(data, path)
    return data


def load_spec(spec_path: str | Path) -> dict[str, Any]:
    """Load a validated spec and project evaluation-v1 for legacy runtimes."""

    return materialize_legacy_spec(load_raw_spec(spec_path))


def resolve_repo(spec: dict[str, Any], spec_path: Path, repo_arg: str | None) -> Path:
    if repo_arg:
        return Path(repo_arg).expanduser().resolve()
    repo_value = spec.get("repo")
    if repo_value:
        repo_path = Path(str(repo_value)).expanduser()
        if not repo_path.is_absolute():
            repo_path = (spec_path.parent / repo_path).resolve()
        return repo_path
    benchmark = spec.get("benchmark")
    if benchmark:
        resolved_spec = spec_path.resolve()
        root = (
            resolved_spec.parents[2]
            if len(resolved_spec.parents) >= 3 and resolved_spec.parent.parent.name == "specs"
            else Path.cwd()
        )
        candidates = {
            "2048": Path("games/2048"),
            "spacerpg": Path("games/SpaceRPG"),
            "spacerpg": Path("games/SpaceRPG"),
            "SpaceRPG": Path("games/SpaceRPG"),
            "love": Path("games/DatingSim"),
            "datingsim": Path("games/DatingSim"),
            "DatingSim": Path("games/DatingSim"),
            "openra": Path("games/OpenRA"),
            "OpenRA": Path("games/OpenRA"),
            "jupyterlab": Path("games/jupyterlab"),
            "JupyterLab": Path("games/jupyterlab"),
            "libreoffice": Path("games/LibreOffice"),
            "LibreOffice": Path("games/LibreOffice"),
            "blender": Path("games/blender"),
            "Blender": Path("games/blender"),
            "jynew": Path("games/jynew"),
            "pal3": Path("games/Pal3.Unity"),
            "Pal3.Unity": Path("games/Pal3.Unity"),
            "luanti": Path("games/luanti"),
            "minetest": Path("games/luanti"),
        }
        if benchmark in candidates:
            return (root / candidates[benchmark]).resolve()
    raise ValueError("No repository resolved. Pass `--repo <repo_path>` or declare `repo:`/`benchmark:` in the spec.")


def run_spec(
    spec_path: str | Path,
    repo_arg: str | None = None,
    mode: str = "jump_and_validate",
    report_dir: Path | None = None,
    headless: bool = True,
    visual_required: bool = False,
    oracle_policy: str | None = None,
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
    experimental_hook: Callable[[dict[str, Any], Path, Any, RunReport], None]
    | None = None,
) -> RunReport:
    spec_file = Path(spec_path).expanduser().resolve()
    spec = load_spec(spec_file)
    repo = resolve_repo(spec, spec_file, repo_arg)
    # Keep the mutable runtime phase isolated per source checkout.  This is a
    # no-op for unrelated repositories, so parallel P5 model jobs can still
    # overlap their LLM work and runs against different benchmark families.
    with runtime_lease(repo):
        return _run_spec_unlocked(
            spec_file,
            spec,
            repo,
            mode=mode,
            report_dir=report_dir,
            headless=headless,
            visual_required=visual_required,
            oracle_policy=oracle_policy,
            visual_sample_rate=visual_sample_rate,
            visual_on_semantic_failure=visual_on_semantic_failure,
            cascade_config=cascade_config,
            experimental_hook=experimental_hook,
        )


def _run_spec_unlocked(
    spec_file: Path,
    spec: dict[str, Any],
    repo: Path,
    *,
    mode: str,
    report_dir: Path | None,
    headless: bool,
    visual_required: bool,
    oracle_policy: str | None,
    visual_sample_rate: float,
    visual_on_semantic_failure: bool,
    cascade_config: CascadePolicyConfig | None,
    experimental_hook: Callable[[dict[str, Any], Path, Any, RunReport], None]
    | None,
) -> RunReport:
    policy = oracle_policy_config(
        oracle_policy=oracle_policy,
        visual_required=visual_required,
        visual_sample_rate=visual_sample_rate,
        visual_on_semantic_failure=visual_on_semantic_failure,
    )
    cascade = cascade_config or cascade_policy_config()
    if spec.get("benchmark") == "jynew":
        ensure_jynew_instrumentation(repo)
    if spec.get("benchmark") == "pal3":
        ensure_pal3_instrumentation(repo)
    if spec.get("benchmark") in {"luanti", "minetest"}:
        ensure_luanti_instrumentation(repo)
    inspector = RepositoryInspector()
    inspect_result = inspector.inspect(repo)
    profile = profile_for(inspect_result)
    out_dir = report_dir or new_report_dir()
    if inspect_result.profile == "JynewProfile":
        visual_capture = policy_may_capture_visual(policy, spec)
        with JynewRuntimeAdapter(repo, out_dir, headless=headless, visual_capture=visual_capture) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    if inspect_result.profile == "Pal3Profile":
        visual_capture = policy_may_capture_visual(policy, spec)
        with Pal3RuntimeAdapter(repo, out_dir, headless=headless, visual_capture=visual_capture) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    if inspect_result.profile == "OpenRAProfile":
        with OpenRARuntimeAdapter(repo, out_dir) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    if inspect_result.profile == "JupyterLabProfile":
        with JupyterLabRuntimeAdapter(repo, out_dir, headless=headless) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    if inspect_result.profile == "LibreOfficeProfile":
        visual_capture = policy_may_capture_visual(policy, spec)
        with LibreOfficeRuntimeAdapter(repo, out_dir, headless=headless, visual_capture=visual_capture) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    if inspect_result.profile == "BlenderProfile":
        with BlenderRuntimeAdapter(repo, out_dir, headless=headless) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    if inspect_result.profile == "LuantiProfile":
        visual_capture = policy_may_capture_visual(policy, spec)
        with LuantiRuntimeAdapter(repo, out_dir, headless=headless, visual_capture=visual_capture) as runtime:
            report = profile.run(spec, spec_file, runtime, mode=mode)
            if experimental_hook is not None:
                experimental_hook(spec, spec_file, runtime, report)
            apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
            report.app_url = runtime.app_url
            report.launch_command = runtime.launch_command
            report.logs.extend(runtime.logs)
            write_run_report(report, out_dir)
            return report
    with BrowserRuntimeAdapter(repo, inspect_result.metadata.launch_command, out_dir, headless=headless) as runtime:
        report = profile.run(spec, spec_file, runtime, mode=mode)
        if experimental_hook is not None:
            experimental_hook(spec, spec_file, runtime, report)
        apply_oracle_policy(config=policy, cascade_config=cascade, spec=spec, spec_path=spec_file, runtime=runtime, report=report)
        report.app_url = runtime.app_url
        report.launch_command = runtime.launch_command
        if runtime.server_log.exists():
            report.logs.append(str(runtime.server_log))
        write_run_report(report, out_dir)
        return report


def compare_spec(
    spec_path: str | Path,
    repo_arg: str | None = None,
    headless: bool = True,
    report_dir: Path | None = None,
    visual_required: bool = False,
    oracle_policy: str | None = None,
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
) -> dict[str, Any]:
    out_dir = report_dir or new_report_dir()
    results: dict[str, Any] = {"modes": {}, "report_dir": str(out_dir)}
    for mode in ABLATION_MODES:
        subdir = out_dir / mode
        subdir.mkdir(parents=True, exist_ok=True)
        try:
            report = run_spec(
                spec_path,
                repo_arg=repo_arg,
                mode=mode,
                report_dir=subdir,
                headless=headless,
                visual_required=visual_required,
                oracle_policy=oracle_policy,
                visual_sample_rate=visual_sample_rate,
                visual_on_semantic_failure=visual_on_semantic_failure,
                cascade_config=cascade_config,
            )
            results["modes"][mode] = report.to_dict()
        except Exception as error:
            reason = str(error)
            results["modes"][mode] = {"status": "unsupported" if is_unsupported_reason(reason) else "failed", "reason": reason}
    results["summary"] = compare_summary(results["modes"])
    write_json(out_dir / "compare.json", results)
    return results


def compare_summary(modes: dict[str, Any]) -> dict[str, Any]:
    jump = modes.get("jump_and_validate")
    replay = modes.get("replay_from_start")
    no_validation = modes.get("inject_or_restore_no_validation")
    summary: dict[str, Any] = {"modes": {}}
    for mode, result in modes.items():
        if isinstance(result, dict):
            summary["modes"][mode] = compact_mode_result(result)
    if isinstance(jump, dict) and isinstance(replay, dict):
        jump_ms = jump.get("metrics", {}).get("time_to_target_ms")
        replay_ms = replay.get("metrics", {}).get("time_to_target_ms")
        # Only count a speedup when both modes actually reached the target. A replay
        # run that errored or was unsupported still records a partial time, and
        # including it would bias the speedup ratio downward.
        replay_ok = _mode_outcome(replay) == "success"
        jump_ok = _mode_outcome(jump) == "success"
        if replay_ok and jump_ok and isinstance(jump_ms, int) and jump_ms > 0 and isinstance(replay_ms, int):
            summary["speedup_vs_replay"] = replay_ms / jump_ms
            summary["jump_and_validate_time_to_target_ms"] = jump_ms
            summary["replay_time_to_target_ms"] = replay_ms
        elif not replay_ok:
            summary["replay_excluded_from_speedup"] = {
                "reason": "replay_from_start did not reach the target",
                "replay_status": _mode_outcome(replay),
            }
    if isinstance(jump, dict) and isinstance(no_validation, dict):
        jump_ms = jump.get("metrics", {}).get("time_to_target_ms")
        no_validation_ms = no_validation.get("metrics", {}).get("time_to_target_ms")
        if isinstance(jump_ms, int) and isinstance(no_validation_ms, int):
            summary["validation_overhead_ms"] = jump_ms - no_validation_ms
            if no_validation_ms > 0:
                summary["validation_overhead_ratio"] = jump_ms / no_validation_ms
    return summary


def compare_benchmark_specs(
    spec_paths: list[Path],
    headless: bool = True,
    report_dir: Path | None = None,
    visual_required: bool = False,
    oracle_policy: str | None = None,
    visual_sample_rate: float = 0.0,
    visual_on_semantic_failure: bool = False,
    cascade_config: CascadePolicyConfig | None = None,
) -> dict[str, Any]:
    out_dir = report_dir or new_report_dir()
    results: list[dict[str, Any]] = []
    for spec_path in spec_paths:
        spec_file = Path(spec_path).expanduser().resolve()
        result: dict[str, Any] = {"spec": str(spec_file)}
        try:
            spec = load_spec(spec_file)
            result["benchmark"] = spec.get("benchmark", "unknown")
            compare_dir = out_dir / "compare" / spec_file.parent.name / spec_file.stem
            compare = compare_spec(
                spec_file,
                repo_arg=None,
                headless=headless,
                report_dir=compare_dir,
                visual_required=visual_required,
                oracle_policy=oracle_policy,
                visual_sample_rate=visual_sample_rate,
                visual_on_semantic_failure=visual_on_semantic_failure,
                cascade_config=cascade_config,
            )
            result["compare_report"] = compare["report_dir"]
            result["modes"] = {mode: compact_mode_result(mode_result) for mode, mode_result in compare["modes"].items()}
            result["summary"] = compare["summary"]
        except Exception as error:
            result.update({"benchmark": "unknown", "status": "failed", "reason": str(error)})
        results.append(result)
    payload = {"report_dir": str(out_dir), "results": results, "summary": summarize_benchmark_comparisons(results)}
    write_json(out_dir / "bench-compare.json", payload)
    return payload


def compact_mode_result(result: dict[str, Any]) -> dict[str, Any]:
    metrics = result.get("metrics") if isinstance(result.get("metrics"), dict) else {}
    visual = result.get("visual_oracle_result") if isinstance(result.get("visual_oracle_result"), dict) else {}
    compact = {
        "status": _mode_outcome(result),
        "raw_status": result.get("overall_status") or result.get("status"),
        "reason": result.get("reason", ""),
        "target_validated_before_smoke": result.get("target_validated_before_smoke"),
        "checkpoint_strategy": result.get("checkpoint_strategy"),
        "checkpoint_validity_class": result.get("checkpoint_validity_class"),
        "time_to_target_ms": metrics.get("time_to_target_ms"),
        "time_to_first_action_ms": metrics.get("time_to_first_action_ms"),
        "actions_replayed": metrics.get("actions_replayed"),
        "semantic_status": result.get("semantic_status"),
        "visual_oracle_status": visual.get("status"),
        "oracle_policy": result.get("oracle_policy"),
        "visual_audit_reason": result.get("visual_audit_reason"),
    }
    unsupported = result.get("unsupported_baseline_reasons")
    if unsupported:
        compact["unsupported_baseline_reasons"] = unsupported
    return compact


def summarize_benchmark_comparisons(results: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total_specs": len(results),
        "benchmarks": {},
        "modes": {mode: {"total": 0, "success": 0, "failure": 0, "unsupported": 0, "failed": 0, "other": 0} for mode in ABLATION_MODES},
        "speedup_vs_replay": {"available": 0, "mean": None, "median": None, "values": []},
        "validation_overhead_ms": {"available": 0, "mean": None, "median": None, "values": []},
    }
    speedups: list[float] = []
    overheads: list[int] = []
    for result in results:
        benchmark = str(result.get("benchmark") or "unknown")
        bucket = summary["benchmarks"].setdefault(benchmark, {"total": 0, "success": 0, "failure": 0})
        bucket["total"] += 1
        modes = result.get("modes")
        if not isinstance(modes, dict):
            bucket["failure"] += 1
            continue
        jump_status = modes.get("jump_and_validate", {}).get("status") if isinstance(modes.get("jump_and_validate"), dict) else None
        if jump_status == "success":
            bucket["success"] += 1
        else:
            bucket["failure"] += 1
        for mode in ABLATION_MODES:
            mode_result = modes.get(mode)
            if not isinstance(mode_result, dict):
                continue
            outcome = str(mode_result.get("status") or "other")
            mode_bucket = summary["modes"][mode]
            mode_bucket["total"] += 1
            if outcome in mode_bucket:
                mode_bucket[outcome] += 1
            else:
                mode_bucket["other"] += 1
        result_summary = result.get("summary")
        if isinstance(result_summary, dict):
            speedup = result_summary.get("speedup_vs_replay")
            if isinstance(speedup, int | float):
                speedups.append(float(speedup))
            overhead = result_summary.get("validation_overhead_ms")
            if isinstance(overhead, int):
                overheads.append(overhead)
    _fill_numeric_summary(summary["speedup_vs_replay"], speedups)
    _fill_numeric_summary(summary["validation_overhead_ms"], overheads)
    return summary


def _fill_numeric_summary(bucket: dict[str, Any], values: list[int] | list[float]) -> None:
    bucket["available"] = len(values)
    bucket["values"] = values
    if not values:
        return
    sorted_values = sorted(values)
    bucket["mean"] = sum(values) / len(values)
    middle = len(sorted_values) // 2
    if len(sorted_values) % 2:
        bucket["median"] = sorted_values[middle]
    else:
        bucket["median"] = (sorted_values[middle - 1] + sorted_values[middle]) / 2


def _mode_outcome(result: dict[str, Any]) -> str:
    status = result.get("overall_status") or result.get("status") or "unknown"
    reason = str(result.get("reason", "")).lower()
    if status in {"failed", "unsupported"} and is_unsupported_reason(reason):
        return "unsupported"
    if status in {"success", "failure", "failed"}:
        return status
    return "other"


def prepare_repo(repo: Path, install_deps: bool = False) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    files_modified: list[str] = []
    if repo.name.lower() == "jynew" or (repo / "jyx2" / "Assets").is_dir():
        instrumentation = ensure_jynew_instrumentation(repo)
        actions.append({"action": "install jynew instrumentation", **instrumentation})
        if instrumentation.get("installed"):
            files_modified.append("jyx2/Assets/Scripts/GameCore/WarptestCheckpointCommand.cs")
    if "pal3" in repo.name.lower() or (repo / "Assets" / "Scripts" / "Pal3.Game").is_dir():
        instrumentation = ensure_pal3_instrumentation(repo)
        actions.append({"action": "install pal3 instrumentation", **instrumentation})
        if instrumentation.get("installed"):
            files_modified.append("Assets/Scripts/Pal3.Game/Command/WarptestCheckpointCommand.cs")
    if repo.name.lower() in {"luanti", "minetest"} or (
        (repo / "builtin" / "init.lua").exists() and (repo / "games" / "devtest").is_dir()
    ):
        instrumentation = ensure_luanti_instrumentation(repo)
        actions.append({"action": "install luanti instrumentation", **instrumentation})
        if instrumentation.get("installed"):
            files_modified.extend(["mods/warptest/", "src/client/game.cpp"])
    package = repo / "package.json"
    node_modules = repo / "node_modules"
    had_node_modules = node_modules.exists()
    if package.exists():
        if install_deps:
            command = ["npm", "ci"] if (repo / "package-lock.json").exists() else ["npm", "install"]
            result = subprocess.run(command, cwd=str(repo), text=True, capture_output=True)
            actions.append({"action": " ".join(command), "returncode": result.returncode, "node_modules_before": had_node_modules, "node_modules_after": node_modules.exists()})
            if node_modules.exists() and not had_node_modules:
                files_modified.append("node_modules/")
        else:
            actions.append({"action": "no install", "reason": "Pass --install-deps with --prepare to install package dependencies."})
    else:
        actions.append({"action": "no install", "reason": "No package.json"})
    return {"repo": str(repo), "actions": actions, "files_modified": files_modified}
