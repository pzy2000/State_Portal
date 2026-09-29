from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core_experiment import gui_from_start_baseline
from .discovery import RepositoryInspector
from .effort import BENCHMARK_REPOS, build_effort_report
from .gui import GenericGuiAppAdapter
from .reports import write_json
from .runner import ABLATION_MODES, compare_benchmark_specs, load_spec
from .segments import load_hidden_bugs, load_public_segments, validate_public_segment_bank
from .synthesis import DeterministicCheckpointSynthesisAgent
from .strategies import SPACERPG_REPLAY_UNSUPPORTED_REASON, LIBREOFFICE_REPLAY_UNSUPPORTED_REASON
from .task_schema import load_hidden_oracles, load_public_issues, resolve_hidden_spec, validate_method_checkpoint, validate_public_tasks_do_not_reference_hidden


_BENCHMARK_DISPLAY_NAMES = {
    "2048": "2048",
    "blender": "Blender",
    "spacerpg": "SpaceRPG",
    "jupyterlab": "JupyterLab",
    "datingsim": "DatingSim",
    "libreoffice": "LibreOffice",
    "luanti": "luanti",
    "openra": "OpenRA",
    "jynew": "jynew",
    "pal3": "pal3",
}


def build_paper_tables(
    root: str | Path = Path.cwd(),
    *,
    generate_missing: bool = False,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    spec_paths = sorted((base / "specs").glob("*/*.yaml"))
    coverage = _coverage_table(base, spec_paths)
    bench_compare = _load_or_generate_bench_compare(base, spec_paths, generate_missing=generate_missing, report_dir=report_dir)
    effort = build_effort_report(base)
    tables = {
        "benchmark_spec_coverage": coverage,
        "core_public_task_coverage": _core_public_task_table(base),
        "leakage_control": _leakage_control_table(base),
        "llm_synthesis_results": _llm_synthesis_results_table(base),
        "method_checkpoint_validity": _method_checkpoint_validity_table(base),
        "baseline_fairness": _baseline_fairness_table(base),
        "spacerpg_gui_baseline_results": _spacerpg_gui_baseline_results_table(base),
        "electron_gui_boundary": _electron_gui_boundary_table(base),
        "segment_bank": _segment_bank_table(base),
        "segment_bug_discovery": _segment_bug_discovery_table(base),
        "ablation_mode_summary": _ablation_summary_table(bench_compare),
        "replay_speedup": _replay_speedup_table(bench_compare),
        "unsupported_baselines": _unsupported_baseline_table(coverage, bench_compare),
        "adapter_profile_effort_loc": _effort_table(effort),
        "capability_discovery_evidence": _capability_evidence_table(base),
    }
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "sources": {
            "spec_count": len(spec_paths),
            "bench_compare": bench_compare.get("source") if isinstance(bench_compare, dict) else None,
            "effort": "generated",
            "inspect": "generated",
        },
        "notes": {
            "visual_judge": "Paper tables use semantic validation, smoke oracles, runtime reports, and cached ablations; visual judge is auxiliary.",
            "spacerpg_replay": SPACERPG_REPLAY_UNSUPPORTED_REASON,
        },
        "tables": tables,
    }


def write_paper_tables(out_dir: Path, payload: dict[str, Any]) -> tuple[Path, Path]:
    json_path = out_dir / "paper-tables.json"
    md_path = out_dir / "paper-tables.md"
    write_json(json_path, payload)
    md_path.write_text(render_paper_tables_markdown(payload), encoding="utf-8")
    return json_path, md_path


def render_paper_tables_markdown(payload: dict[str, Any]) -> str:
    tables = payload.get("tables", {})
    sections = [
        "# WarpTest Paper Tables",
        "",
        f"- Generated: `{payload.get('generated_at_utc')}`",
        f"- Root: `{payload.get('root')}`",
        f"- SpaceRPG replay baseline: {payload.get('notes', {}).get('spacerpg_replay')}",
        "",
        "## Benchmark/spec coverage",
        _markdown_table(tables.get("benchmark_spec_coverage", []), ["benchmark", "spec", "expected_validity", "strategies", "has_replay", "has_visual_oracle"]),
        "",
        "## Core public tasks",
        _markdown_table(tables.get("core_public_task_coverage", []), ["task_id", "benchmark", "public_issue_chars", "repo", "has_hidden_oracle"]),
        "",
        "## Leakage control",
        _markdown_table(tables.get("leakage_control", []), ["check", "status", "detail"]),
        "",
        "## Method checkpoint validity",
        _markdown_table(tables.get("method_checkpoint_validity", []), ["task_id", "benchmark", "validity_class", "payload_kind", "source_evidence_count", "runtime_validation_status", "schema_errors"]),
        "",
        "## LLM synthesis results",
        _markdown_table(tables.get("llm_synthesis_results", []), ["task_id", "benchmark", "agent", "model", "grade", "schema_errors", "prompt_private_reference_count", "output_private_reference_count", "source"]),
        "",
        "## Baseline fairness",
        _markdown_table(tables.get("baseline_fairness", []), ["task_id", "benchmark", "baseline", "public_inputs_only", "strict_no_checkpoint", "status"]),
        "",
        "## SpaceRPG GUI-from-start baseline",
        _markdown_table(tables.get("spacerpg_gui_baseline_results", []), ["task_id", "status", "time_to_target_ms", "actions", "forbidden_action_calls", "source"]),
        "",
        "## Electron GUI boundary",
        _markdown_table(tables.get("electron_gui_boundary", []), ["repo", "runtime_family", "launchable", "checkpointing_status", "probe_status", "unsupported_surfaces"]),
        "",
        "## Segment bank",
        _markdown_table(tables.get("segment_bank", []), ["segment_id", "benchmark", "start_spec", "public", "bank_errors"]),
        "",
        "## Segment bug discovery",
        _markdown_table(tables.get("segment_bug_discovery", []), ["segment_id", "hidden_bug_count", "claim_boundary"]),
        "",
        "## Ablation mode summary",
        _markdown_table(tables.get("ablation_mode_summary", []), ["mode", "total", "success", "failure", "unsupported", "failed", "source"]),
        "",
        "## Replay speedup",
        _markdown_table(tables.get("replay_speedup", []), ["benchmark", "spec", "speedup_vs_replay", "jump_and_validate_ms", "replay_ms"]),
        "",
        "## Unsupported baselines",
        _markdown_table(tables.get("unsupported_baselines", []), ["benchmark", "spec", "mode", "reason", "source"]),
        "",
        "## Adapter/profile effort LOC",
        _markdown_table(tables.get("adapter_profile_effort_loc", []), ["component", "benchmark", "loc", "count"]),
        "",
        "## Capability discovery evidence",
        _markdown_table(tables.get("capability_discovery_evidence", []), ["benchmark", "entry_type", "name", "kind_or_source", "access", "source_path", "matched_symbol", "confidence"]),
        "",
    ]
    return "\n".join(sections)


def _coverage_table(root: Path, spec_paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec_path in spec_paths:
        spec = load_spec(spec_path)
        checkpoint = spec.get("checkpoint") if isinstance(spec.get("checkpoint"), dict) else {}
        visual = spec.get("visual_oracle") if isinstance(spec.get("visual_oracle"), dict) else None
        rows.append(
            {
                "benchmark": _BENCHMARK_DISPLAY_NAMES.get(
                    str(spec.get("benchmark", spec_path.parent.name)).lower(),
                    spec.get("benchmark", spec_path.parent.name),
                ),
                "spec": str(spec_path.relative_to(root)),
                "name": spec.get("name"),
                "tags": ", ".join(spec.get("tags", [])) if isinstance(spec.get("tags"), list) else "",
                "strategies": ", ".join(checkpoint.get("allowed_strategies", [])) if isinstance(checkpoint.get("allowed_strategies"), list) else "",
                "expected_validity": checkpoint.get("expected_validity"),
                "has_replay": "replay_from_start" in spec,
                "has_visual_oracle": visual is not None,
            }
        )
    return rows


def _core_public_task_table(root: Path) -> list[dict[str, Any]]:
    hidden = load_hidden_oracles(root)
    rows = []
    for task in load_public_issues(root):
        rows.append(
            {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "public_issue_chars": len(task.issue),
                "repo": task.repo,
                "has_hidden_oracle": task.id in hidden,
            }
        )
    return rows


def _leakage_control_table(root: Path) -> list[dict[str, Any]]:
    public_errors = validate_public_tasks_do_not_reference_hidden(root)
    segment_errors = validate_public_segment_bank(root)
    return [
        {"check": "public_issue_no_hidden_refs", "status": "success" if not public_errors else "failure", "detail": "; ".join(public_errors)},
        {"check": "public_segment_bank_no_hidden_refs", "status": "success" if not segment_errors else "failure", "detail": "; ".join(segment_errors)},
        {"check": "core_claim_separates_segment_bank", "status": "success", "detail": "Segment bank is reported only for segment active bug discovery, not checkpoint synthesis."},
    ]


def _method_checkpoint_validity_table(root: Path) -> list[dict[str, Any]]:
    agent = DeterministicCheckpointSynthesisAgent()
    rows = []
    for task in load_public_issues(root):
        checkpoint = agent.synthesize(task, root)
        errors = validate_method_checkpoint(checkpoint, root)
        payload = checkpoint.payload
        rows.append(
            {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "validity_class": checkpoint.validity_class,
                "payload_kind": payload.get("kind"),
                "source_evidence_count": len(checkpoint.source_evidence),
                "runtime_validation_status": checkpoint.runtime_validation.get("status"),
                "schema_errors": len(errors),
            }
        )
    return rows


def _llm_synthesis_results_table(root: Path) -> list[dict[str, Any]]:
    latest = _latest_core_synthesis_report(root, agent="llm")
    if latest is None:
        return [
            {
                "task_id": "missing_llm_report",
                "benchmark": "all",
                "agent": "llm",
                "model": "",
                "grade": "missing_report",
                "schema_errors": "",
                "prompt_private_reference_count": "",
                "output_private_reference_count": "",
                "source": "no cached core-synthesis report with synthesis_agent=llm",
            }
        ]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for result in payload.get("results", []):
        if not isinstance(result, dict):
            continue
        synthesis = result.get("synthesis") if isinstance(result.get("synthesis"), dict) else {}
        rows.append(
            {
                "task_id": result.get("task_id"),
                "benchmark": result.get("benchmark"),
                "agent": result.get("synthesis_agent") or payload.get("synthesis_agent"),
                "model": synthesis.get("model") or payload.get("synthesis_model") or "",
                "grade": result.get("grading", {}).get("status") if isinstance(result.get("grading"), dict) else "",
                "schema_errors": len(result.get("schema_errors", [])) if isinstance(result.get("schema_errors"), list) else result.get("schema_errors", ""),
                "prompt_private_reference_count": synthesis.get("prompt_private_reference_count", ""),
                "output_private_reference_count": synthesis.get("output_private_reference_count", ""),
                "source": str(latest.relative_to(root)),
            }
        )
    return rows


def _baseline_fairness_table(root: Path) -> list[dict[str, Any]]:
    hidden = load_hidden_oracles(root)
    rows = []
    for task in load_public_issues(root):
        hidden_spec = None
        if task.id in hidden:
            hidden_spec = load_spec(resolve_hidden_spec(root, hidden[task.id]))
        baseline = gui_from_start_baseline(task, hidden_spec, execute=False, enabled=False)
        rows.append(
            {
                "task_id": task.id,
                "benchmark": task.benchmark,
                "baseline": "gui_from_start",
                "public_inputs_only": baseline.get("public_inputs_only"),
                "strict_no_checkpoint": baseline.get("strict_no_checkpoint"),
                "status": baseline.get("status"),
            }
        )
    return rows


def _spacerpg_gui_baseline_results_table(root: Path) -> list[dict[str, Any]]:
    latest = _latest_report(root, "core-synthesis.json")
    if latest is None:
        return [{"task_id": "missing", "status": "missing", "time_to_target_ms": "", "actions": "", "forbidden_action_calls": "", "source": "missing"}]
    payload = json.loads(latest.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for result in payload.get("results", []):
        if not isinstance(result, dict) or result.get("benchmark") != "SpaceRPG":
            continue
        baseline = result.get("baselines", {}).get("gui_from_start", {}) if isinstance(result.get("baselines"), dict) else {}
        trace = baseline.get("action_trace", []) if isinstance(baseline, dict) else []
        rows.append(
            {
                "task_id": result.get("task_id"),
                "status": baseline.get("status"),
                "time_to_target_ms": baseline.get("time_to_target_ms"),
                "actions": len(trace) if isinstance(trace, list) else 0,
                "forbidden_action_calls": ", ".join(baseline.get("forbidden_action_calls", [])) if isinstance(baseline.get("forbidden_action_calls"), list) else "",
                "source": str(latest.relative_to(root)),
            }
        )
    return rows


def _electron_gui_boundary_table(root: Path) -> list[dict[str, Any]]:
    candidates = [
        root / "games" / "2048",
        root / "games" / "SpaceRPG",
        root / "tests" / "fixtures" / "electron_app",
    ]
    rows: list[dict[str, Any]] = []
    adapter = GenericGuiAppAdapter()
    for candidate in candidates:
        if not candidate.exists():
            continue
        inspection = adapter.inspect(candidate).to_dict()
        rows.append(
            {
                "repo": str(candidate.relative_to(root)),
                "runtime_family": inspection.get("runtime_family"),
                "launchable": inspection.get("launchable"),
                "checkpointing_status": inspection.get("checkpointing_status"),
                "probe_status": inspection.get("runtime_probe", {}).get("status") if isinstance(inspection.get("runtime_probe"), dict) else "",
                "unsupported_surfaces": ", ".join(inspection.get("unsupported_surfaces", [])),
            }
        )
    return rows


def _segment_bank_table(root: Path) -> list[dict[str, Any]]:
    errors = validate_public_segment_bank(root)
    return [
        {
            "segment_id": segment.id,
            "benchmark": segment.benchmark,
            "start_spec": segment.start_spec,
            "public": True,
            "bank_errors": len(errors),
        }
        for segment in load_public_segments(root)
    ]


def _segment_bug_discovery_table(root: Path) -> list[dict[str, Any]]:
    bugs = load_hidden_bugs(root)
    counts: dict[str, int] = {}
    for bug in bugs:
        counts[bug.segment_id] = counts.get(bug.segment_id, 0) + 1
    return [
        {
            "segment_id": segment.id,
            "hidden_bug_count": counts.get(segment.id, 0),
            "claim_boundary": "segment-based testing only",
        }
        for segment in load_public_segments(root)
    ]


def _load_or_generate_bench_compare(
    root: Path,
    spec_paths: list[Path],
    *,
    generate_missing: bool,
    report_dir: Path | None,
) -> dict[str, Any]:
    latest = _latest_report(root, "bench-compare.json")
    if latest is not None:
        return {**json.loads(latest.read_text(encoding="utf-8")), "source": str(latest.relative_to(root))}
    if generate_missing:
        out_dir = report_dir or (root / "reports" / "warptest" / "paper-tables-generated")
        return {**compare_benchmark_specs(spec_paths, headless=True, report_dir=out_dir / "bench-compare"), "source": "generated"}
    return {"source": "missing", "summary": {}, "results": []}


def _latest_report(root: Path, filename: str) -> Path | None:
    reports_root = root / "reports" / "warptest"
    if not reports_root.exists():
        return None
    candidates = sorted(reports_root.glob(f"**/{filename}"), key=lambda path: path.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _latest_core_synthesis_report(root: Path, *, agent: str | None = None) -> Path | None:
    reports_root = root / "reports" / "warptest"
    if not reports_root.exists():
        return None
    candidates = sorted(reports_root.glob("**/core-synthesis.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    if agent is None:
        return candidates[0] if candidates else None
    for candidate in candidates:
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get("synthesis_agent") == agent:
            return candidate
    return None


def _ablation_summary_table(bench_compare: dict[str, Any]) -> list[dict[str, Any]]:
    summary = bench_compare.get("summary") if isinstance(bench_compare.get("summary"), dict) else {}
    modes = summary.get("modes") if isinstance(summary.get("modes"), dict) else {}
    rows: list[dict[str, Any]] = []
    for mode in ABLATION_MODES:
        bucket = modes.get(mode, {}) if isinstance(modes.get(mode), dict) else {}
        rows.append(
            {
                "mode": mode,
                "total": bucket.get("total", 0),
                "success": bucket.get("success", 0),
                "failure": bucket.get("failure", 0),
                "unsupported": bucket.get("unsupported", 0),
                "failed": bucket.get("failed", 0),
                "source": bench_compare.get("source", "missing"),
            }
        )
    return rows


def _replay_speedup_table(bench_compare: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in bench_compare.get("results", []):
        if not isinstance(result, dict):
            continue
        summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
        speedup = summary.get("speedup_vs_replay")
        if not isinstance(speedup, int | float):
            continue
        rows.append(
            {
                "benchmark": result.get("benchmark"),
                "spec": result.get("spec"),
                "speedup_vs_replay": speedup,
                "jump_and_validate_ms": summary.get("jump_and_validate_time_to_target_ms"),
                "replay_ms": summary.get("replay_time_to_target_ms"),
            }
        )
    return rows


def _unsupported_baseline_table(coverage: list[dict[str, Any]], bench_compare: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in coverage:
        benchmark = str(row["benchmark"])
        spec = str(row["spec"])
        if benchmark == "SpaceRPG":
            reason = SPACERPG_REPLAY_UNSUPPORTED_REASON
        elif benchmark == "LibreOffice":
            reason = LIBREOFFICE_REPLAY_UNSUPPORTED_REASON
        elif not row["has_replay"]:
            reason = "Spec does not declare replay_from_start baseline."
        else:
            continue
        key = (benchmark, spec, "replay_from_start")
        seen.add(key)
        rows.append({"benchmark": benchmark, "spec": spec, "mode": "replay_from_start", "reason": reason, "source": "static_spec_scan"})
    for result in bench_compare.get("results", []):
        if not isinstance(result, dict):
            continue
        modes = result.get("modes") if isinstance(result.get("modes"), dict) else {}
        for mode, mode_result in modes.items():
            if not isinstance(mode_result, dict) or mode_result.get("status") != "unsupported":
                continue
            key = (str(result.get("benchmark")), str(result.get("spec")), str(mode))
            if key in seen:
                continue
            rows.append(
                {
                    "benchmark": result.get("benchmark"),
                    "spec": result.get("spec"),
                    "mode": mode,
                    "reason": mode_result.get("reason") or mode_result.get("unsupported_baseline_reasons"),
                    "source": bench_compare.get("source", "bench_compare"),
                }
            )
    return rows


def _effort_table(effort: dict[str, Any]) -> list[dict[str, Any]]:
    loc = effort.get("loc", {})
    rows = [
        {"component": "reusable_core_and_adapters", "benchmark": "all", "loc": loc.get("reusable_core_and_adapters"), "count": len(loc.get("reusable_files", {}))},
        {"component": "base_profile_helpers", "benchmark": "all", "loc": loc.get("base_profile_helpers", {}).get("loc"), "count": 1},
        {"component": "specs_total", "benchmark": "all", "loc": loc.get("specs_total"), "count": sum(item.get("count", 0) for item in loc.get("specs", {}).values())},
    ]
    for benchmark, stats in loc.get("game_profiles", {}).items():
        rows.append({"component": "game_profile", "benchmark": benchmark, "loc": stats.get("loc"), "count": 1})
    for benchmark, stats in loc.get("specs", {}).items():
        rows.append({"component": "specs", "benchmark": benchmark, "loc": stats.get("loc"), "count": stats.get("count")})
    return rows


def _capability_evidence_table(root: Path) -> list[dict[str, Any]]:
    inspector = RepositoryInspector()
    rows: list[dict[str, Any]] = []
    for benchmark, repo_rel in BENCHMARK_REPOS.items():
        repo = root / repo_rel
        if not repo.exists():
            continue
        inspect_result = inspector.inspect(repo)
        for surface in inspect_result.semantic_schema.surfaces:
            rows.append(_evidence_row(benchmark, "surface", surface.name, surface.kind, surface.access, surface.evidence))
        for field in inspect_result.semantic_schema.fields:
            rows.append(_evidence_row(benchmark, "field", field.name, field.source, field.readable_via + field.writable_via, field.evidence))
    return rows


def _evidence_row(benchmark: str, entry_type: str, name: str, kind_or_source: str, access: list[str], evidence: list[Any]) -> dict[str, Any]:
    first = asdict(evidence[0]) if evidence else {}
    return {
        "benchmark": benchmark,
        "entry_type": entry_type,
        "name": name,
        "kind_or_source": kind_or_source,
        "access": ", ".join(access),
        "source_path": first.get("source_path"),
        "matched_symbol": first.get("matched_symbol"),
        "confidence": first.get("confidence"),
    }


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No rows available._"
    header = "| " + " | ".join(columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = ["| " + " | ".join(_cell(row.get(column)) for column in columns) + " |" for row in rows]
    return "\n".join([header, divider, *body])


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = str(value).replace("\n", " ")
    return text.replace("|", "\\|")
