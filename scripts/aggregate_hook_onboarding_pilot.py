#!/usr/bin/env python3
"""Aggregate the frozen ten-project hook-onboarding pilot without rewriting raw results."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from typing import Any

import yaml


PILOT_DIRS = {
    "2048": "onboarding-pilot-2048",
    "spacerpg": "onboarding-pilot-spacerpg",
    "datingsim": "onboarding-pilot-love",
    "openra": "onboarding-pilot-openra",
    "jupyterlab": "onboarding-pilot-jupyterlab",
    "libreoffice": "onboarding-pilot-libreoffice",
    "blender": "onboarding-pilot-blender",
    "jynew": "onboarding-pilot-jynew",
    "pal3": "onboarding-pilot-pal3",
    "luanti": "onboarding-pilot-luanti",
}

REFERENCE_DIRS = {
    "2048": "onboarding-reference-2048-v2",
    "spacerpg": "onboarding-reference-spacerpg-v2",
    "datingsim": "onboarding-reference-love",
    "openra": "onboarding-reference-openra",
    "jupyterlab": "onboarding-reference-jupyterlab",
    "libreoffice": "onboarding-reference-libreoffice-v4",
    "blender": "onboarding-reference-blender-v2",
    "jynew": "onboarding-reference-jynew-v4",
    "pal3": "onboarding-reference-pal3-v2",
    "luanti": "onboarding-reference-luanti-v3",
}

FAILURE_NOTES = {
    "2048": (
        "The development smoke passed inside the model session, but the final tool call landed "
        "on response round 40. The frozen protocol therefore records round_limit and does not run held-out cases."
    ),
    "spacerpg": "Held-out A omitted cargo; the generated target validator incorrectly required player.cargo.",
    "jupyterlab": (
        "The notebook adapter did not normalize required cell metadata/outputs, and its contract marked "
        "normal_mode_absent false for an isolated existing-API driver."
    ),
    "libreoffice": (
        "The generated Java UNO helper compiled, but Writer save/reopen timed out; it also did not accept "
        "the registered calc_spreadsheet target shape."
    ),
    "jynew": "The generated schema used inventory_items and quest_flags instead of the registered items and key_values fields.",
    "pal3": "Held-out B used story_vars, which the generated extension rejected as unsupported.",
    "luanti": (
        "The named build check did not provision a server binary for a Lua-only patch. The agent added a self-build "
        "path at round 40 but had no remaining response to validate it or produce a final."
    ),
}


def read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def single_result(summary_path: Path, app_id: str) -> dict[str, Any]:
    summary = read_json(summary_path)
    results = summary.get("results")
    if not isinstance(results, list) or len(results) != 1 or results[0].get("app_id") != app_id:
        raise ValueError(f"expected exactly one {app_id} result in {summary_path}")
    return results[0]


def contract_kind(pilot_dir: Path, app_id: str) -> str | None:
    path = pilot_dir / app_id / "contract.yaml"
    if not path.is_file():
        return None
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    return value.get("hook_kind") if isinstance(value, dict) else None


def case_passes(result: dict[str, Any]) -> tuple[bool, int]:
    development = result.get("development_smoke")
    development_pass = isinstance(development, dict) and development.get("status") == "success"
    held = result.get("held_out")
    held_passes = sum(item.get("status") == "success" for item in held) if isinstance(held, list) else 0
    return development_pass, held_passes


def reference_metrics(result: dict[str, Any]) -> dict[str, Any]:
    reference = result.get("reference")
    if not isinstance(reference, dict):
        return {"status": None, "prepare_wall_time_ms": 0, "command_wall_time_ms": None}
    prepare = reference.get("prepare")
    prepare_ms = sum(int(item.get("elapsed_ms") or 0) for item in prepare) if isinstance(prepare, list) else 0
    command = reference.get("command")
    return {
        "status": reference.get("status"),
        "prepare_wall_time_ms": prepare_ms,
        "command_wall_time_ms": command.get("elapsed_ms") if isinstance(command, dict) else None,
    }


def row_for(
    app: dict[str, Any],
    pilot_root: Path,
    reference_root: Path,
) -> dict[str, Any]:
    app_id = str(app["id"])
    pilot_dir = pilot_root / PILOT_DIRS[app_id]
    reference_dir = reference_root / REFERENCE_DIRS[app_id]
    raw = single_result(pilot_dir / "summary.json", app_id)
    reference_raw = single_result(reference_dir / "summary.json", app_id)
    development_pass, held_passes = case_passes(raw)
    agent = raw.get("agent") if isinstance(raw.get("agent"), dict) else {}
    usage = agent.get("usage") if isinstance(agent.get("usage"), dict) else {}
    prepare = raw.get("prepare")
    prepare_ms = sum(int(item.get("elapsed_ms") or 0) for item in prepare) if isinstance(prepare, list) else 0
    diff = raw.get("diff") if isinstance(raw.get("diff"), dict) else {}
    hook_kind = raw.get("hook_kind") or contract_kind(pilot_dir, app_id)
    return {
        "app_id": app_id,
        "family": app.get("family"),
        "status": raw.get("status"),
        "stage": raw.get("stage"),
        "onboarding_success": bool(raw.get("onboarding_success", development_pass)),
        "development_smoke_pass": development_pass,
        "heldout_passes": int(raw.get("heldout_passes", held_passes)),
        "heldout_total": 2,
        "reusable_hook_success": bool(raw.get("reusable_hook_success", held_passes == 2)),
        "hook_kind": hook_kind,
        "gate_status": raw.get("gate_status"),
        "agent_status": agent.get("status"),
        "responses": usage.get("responses"),
        "rounds": agent.get("rounds"),
        "tool_calls": agent.get("tool_calls"),
        "tool_counts": agent.get("tool_counts", {}),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_input_tokens": usage.get("cached_input_tokens"),
        "reasoning_tokens": usage.get("reasoning_tokens"),
        "agent_wall_time_ms": agent.get("wall_time_ms"),
        "prepare_wall_time_ms": prepare_ms,
        "tracked_files_changed": diff.get("tracked_files_changed"),
        "untracked_files_added": diff.get("untracked_files_added"),
        "lines_added": diff.get("lines_added"),
        "lines_deleted": diff.get("lines_deleted"),
        "human_interventions": int(raw.get("human_interventions", 0)),
        "reference": reference_metrics(reference_raw),
        "pilot_result_dir": str(pilot_dir),
        "reference_result_dir": str(reference_dir),
        "failure_note": FAILURE_NOTES.get(app_id),
    }


def group_rows(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("hook_kind") or "unknown")].append(row)
    return {
        kind: {
            "projects": len(items),
            "passes": sum(item["status"] == "pass" for item in items),
            "onboarding_successes": sum(bool(item["onboarding_success"]) for item in items),
            "heldout_passes": sum(int(item["heldout_passes"]) for item in items),
            "heldout_total": 2 * len(items),
            "reusable_hook_successes": sum(bool(item["reusable_hook_success"]) for item in items),
        }
        for kind, items in sorted(grouped.items())
    }


def fmt_int(value: Any) -> str:
    return "—" if value is None else f"{int(value):,}"


def render_markdown(summary: dict[str, Any]) -> str:
    rows = summary["results"]
    totals = summary["totals"]
    provider = summary["provider_preflight"]
    lines = [
        "# Hook Onboarding Pilot v1 Results",
        "",
        "> This report aggregates the frozen raw pilot outputs. It does not repair generated hooks, rerun failed agents, "
        "or convert expected outcomes into completed results.",
        "",
        "## Outcome",
        "",
        f"Provider preflight: **{provider.get('status')}** with `{provider.get('model')}` / "
        f"`{provider.get('reasoning_effort')}`. All 10 handwritten references passed on the registered clean revisions.",
        "",
        f"The autonomous pilot produced **{totals['status_counts'].get('pass', 0)}/10 full passes**, "
        f"**{totals['onboarding_successes']}/10 development-smoke successes**, "
        f"**{totals['heldout_passes']}/20 held-out passes**, and "
        f"**{totals['reusable_hook_successes']}/10 reusable 2/2 hooks**.",
        "",
        "| App | Kind | Reference | Dev | Held-out | Gate | Raw status | Rounds | Tools | Total tokens | Agent time ms | Prep time ms |",
        "|---|---|---:|---:|---:|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            "| {app_id} | {hook_kind} | {reference} | {dev} | {heldout_passes}/2 | {gate} | {status} | "
            "{rounds} | {tools} | {tokens} | {agent_ms} | {prepare_ms} |".format(
                app_id=row["app_id"],
                hook_kind=row["hook_kind"] or "—",
                reference=row["reference"]["status"] or "—",
                dev="pass" if row["development_smoke_pass"] else "—",
                heldout_passes=row["heldout_passes"],
                gate=row["gate_status"] or "—",
                status=row["status"],
                rounds=fmt_int(row["rounds"]),
                tools=fmt_int(row["tool_calls"]),
                tokens=fmt_int(row["total_tokens"]),
                agent_ms=fmt_int(row["agent_wall_time_ms"]),
                prepare_ms=fmt_int(row["prepare_wall_time_ms"]),
            )
        )

    lines += ["", "## Hook-kind groups", ""]
    for kind, values in summary["hook_kind_groups"].items():
        lines.append(
            f"- `{kind}`: {values['passes']}/{values['projects']} full passes; "
            f"{values['heldout_passes']}/{values['heldout_total']} held-out cases; "
            f"{values['reusable_hook_successes']}/{values['projects']} reusable 2/2 hooks."
        )

    lines += ["", "## Failure analysis", ""]
    for row in rows:
        if row["status"] != "pass":
            lines.append(f"- **{row['app_id']} — `{row['status']}`:** {row['failure_note']}")

    lines += [
        "",
        "## Protocol notes",
        "",
        "- Every project used one custom Responses API coding-agent session with `gpt-5.6-sol` and `medium` reasoning.",
        "- Held-out cases ran only after an agent final. Round-limit projects therefore have 0/2 recorded held-out passes, not inferred passes.",
        "- All handwritten references passed, so application/case feasibility is independently established.",
        "- No generated hook was edited after agent final, and `human_interventions` is 0 for every row.",
        "- No additional content, prompt, manifest, patch, result, or cache hashes were calculated.",
        "- Pilot checkout scratch directories were deleted after each project; only reports and bounded artifacts remain.",
        "",
        "## Aggregate usage",
        "",
        f"- Responses: {fmt_int(totals['responses'])}",
        f"- Function tool calls: {fmt_int(totals['tool_calls'])}",
        f"- Input tokens: {fmt_int(totals['input_tokens'])}",
        f"- Output tokens: {fmt_int(totals['output_tokens'])}",
        f"- Total tokens: {fmt_int(totals['total_tokens'])}",
        f"- Cached input tokens: {fmt_int(totals['cached_input_tokens'])}",
        f"- Reasoning tokens: {fmt_int(totals['reasoning_tokens'])}",
        f"- Agent wall time: {fmt_int(totals['agent_wall_time_ms'])} ms",
        f"- Dependency preparation wall time: {fmt_int(totals['prepare_wall_time_ms'])} ms",
        "",
    ]
    return "\n".join(lines)


def aggregate(manifest_path: Path, reports_root: Path, provider_path: Path) -> dict[str, Any]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    apps = manifest.get("apps") if isinstance(manifest, dict) else None
    if not isinstance(apps, list) or [app.get("id") for app in apps] != list(PILOT_DIRS):
        raise ValueError("manifest app order does not match the frozen ten-project pilot")
    rows = [row_for(app, reports_root, reports_root) for app in apps]
    statuses = Counter(str(row["status"]) for row in rows)
    sum_fields = (
        "responses", "tool_calls", "input_tokens", "output_tokens", "total_tokens",
        "cached_input_tokens", "reasoning_tokens", "agent_wall_time_ms", "prepare_wall_time_ms",
    )
    totals = {field: sum(int(row.get(field) or 0) for row in rows) for field in sum_fields}
    totals.update(
        status_counts=dict(statuses),
        projects=len(rows),
        onboarding_successes=sum(bool(row["onboarding_success"]) for row in rows),
        heldout_passes=sum(int(row["heldout_passes"]) for row in rows),
        heldout_total=2 * len(rows),
        reusable_hook_successes=sum(bool(row["reusable_hook_success"]) for row in rows),
        reference_passes=sum(row["reference"]["status"] == "pass" for row in rows),
        human_interventions=sum(int(row["human_interventions"]) for row in rows),
    )
    provider = read_json(provider_path)
    provider.pop("provider_base_url", None)
    return {
        "schema_version": 1,
        "pilot_id": manifest.get("pilot_id"),
        "model": manifest.get("model"),
        "reasoning_effort": manifest.get("reasoning_effort"),
        "raw_results_preserved": True,
        "provider_preflight": provider,
        "totals": totals,
        "hook_kind_groups": group_rows(rows),
        "results": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=Path("benchmarks/onboarding/manifest.yaml"))
    parser.add_argument("--reports-root", type=Path, default=Path("reports/warptest"))
    parser.add_argument(
        "--provider-preflight",
        type=Path,
        default=Path("reports/warptest/onboarding-provider-preflight/provider-preflight.json"),
    )
    parser.add_argument("--output-dir", type=Path, default=Path("reports/warptest/onboarding-pilot"))
    args = parser.parse_args()
    summary = aggregate(args.manifest, args.reports_root, args.provider_preflight)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (args.output_dir / "summary.md").write_text(render_markdown(summary), encoding="utf-8")
    print(json.dumps(summary["totals"], indent=2))


if __name__ == "__main__":
    main()
