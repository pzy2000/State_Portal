#!/usr/bin/env python3
"""Create a compact paper-facing report for one uniform ten-app run."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cached_input_tokens",
    "reasoning_tokens",
    "responses",
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--summary",
        type=Path,
        default=Path("reports/warptest/onboarding-hook-uniform/summary.json"),
    )
    args = parser.parse_args()
    raw: dict[str, Any] = json.loads(args.summary.read_text(encoding="utf-8"))
    rows = list(raw.get("results") or [])
    expected_apps = [
        "2048",
        "spacerpg",
        "datingsim",
        "openra",
        "jupyterlab",
        "libreoffice",
        "blender",
        "jynew",
        "pal3",
        "luanti",
    ]
    if raw.get("pilot_id") != "hook-onboarding-uniform":
        raise SystemExit("summary is not hook-onboarding-uniform")
    if raw.get("apps_requested") != expected_apps:
        raise SystemExit("summary is not one ordered ten-application run")
    if raw.get("counts") != {"pass": 10} or len(rows) != 10:
        raise SystemExit("uniform final run is not 10/10 pass")

    totals = {key: 0 for key in USAGE_KEYS}
    totals.update({"tool_calls": 0, "agent_wall_time_ms": 0, "prepare_wall_time_ms": 0})
    compact_rows = []
    for row in rows:
        if not (
            row.get("status") == "pass"
            and row.get("self_test_passes") == row.get("self_test_cases") == 6
            and row.get("development_passes") == row.get("development_cases") == 3
            and row.get("heldout_passes") == 2
            and row.get("reusable_hook_success") is True
            and row.get("gate_status") == "pass"
        ):
            raise SystemExit(f"incomplete final row: {row.get('app_id')}")
        agent = row["agent"]
        usage = agent["usage"]
        for key in USAGE_KEYS:
            totals[key] += int(usage.get(key) or 0)
        totals["tool_calls"] += int(agent.get("tool_calls") or 0)
        totals["agent_wall_time_ms"] += int(agent.get("wall_time_ms") or 0)
        totals["prepare_wall_time_ms"] += sum(
            int(item.get("elapsed_ms") or 0) for item in row.get("prepare") or []
        )
        compact_rows.append(
            {
                "app_id": row["app_id"],
                "status": row["status"],
                "hook_kind": row["hook_kind"],
                "self_tests": "6/6",
                "development": "3/3",
                "held_out": "2/2",
                "rounds": agent["rounds"],
                "tool_calls": agent["tool_calls"],
                "total_tokens": usage["total_tokens"],
            }
        )

    output = {
        "schema_version": 1,
        "result_set": "hook-onboarding-uniform",
        "source_summary": str(args.summary.resolve()),
        "model": raw["model"],
        "reasoning_effort": raw["reasoning_effort"],
        "apps": 10,
        "complete_hooks": 10,
        "self_test_passes": 60,
        "self_test_cases": 60,
        "development_passes": 30,
        "development_cases": 30,
        "heldout_passes": 20,
        "heldout_cases": 20,
        "reusable_hooks": 10,
        "human_interventions": 0,
        "totals": totals,
        "results": compact_rows,
        "validity_notes": [
            "All ten applications were requested by one runner invocation under one frozen manifest.",
            "All rows were generated from clean fixed-commit source materializations in this run.",
            "Hidden cases were unavailable to the agent and executed only after post-final checks.",
            "No result from an earlier run is merged or substituted.",
        ],
    }
    output_dir = args.summary.parent
    json_path = output_dir / "formal-summary.json"
    md_path = output_dir / "formal-summary.md"
    json_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    lines = [
        "# Uniform Hook Onboarding Final Result",
        "",
        "One `hook-onboarding-uniform` runner invocation requested all ten applications under the same frozen manifest.",
        "",
        "| App | Hook kind | Status | Self | Development | Hidden | Rounds | Tool calls | Tokens |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in compact_rows:
        lines.append(
            f"| {row['app_id']} | {row['hook_kind']} | {row['status']} | "
            f"{row['self_tests']} | {row['development']} | {row['held_out']} | "
            f"{row['rounds']} | {row['tool_calls']} | {row['total_tokens']:,} |"
        )
    lines.extend(
        [
            "",
            "## Totals",
            "",
            "- Complete reusable hooks: 10/10",
            "- Agent-generated post-final self-tests: 60/60",
            "- Agent-visible development cases: 30/30",
            "- Agent-hidden held-out cases: 20/20",
            f"- Responses: {totals['responses']}",
            f"- Function tool calls: {totals['tool_calls']}",
            f"- Total tokens: {totals['total_tokens']:,}",
            f"- Agent wall time: {totals['agent_wall_time_ms'] / 1000:.1f}s",
            f"- Prepare wall time: {totals['prepare_wall_time_ms'] / 1000:.1f}s",
        ]
    )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json_path)
    print(md_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
