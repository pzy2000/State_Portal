from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import InspectResult, RunReport


def new_report_dir(base: Path = Path("reports/warptest")) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = base / stamp
    counter = 1
    while path.exists():
        path = base / f"{stamp}-{counter}"
        counter += 1
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_inspect_report(result: InspectResult, path: Path | None = None) -> Path:
    out = path or (new_report_dir() / "inspect.json")
    write_json(out, asdict(result))
    return out


def write_run_report(report: RunReport, report_dir: Path) -> tuple[Path, Path]:
    json_path = report_dir / "report.json"
    md_path = report_dir / "report.md"
    write_json(json_path, report.to_dict())
    md_path.write_text(_markdown(report), encoding="utf-8")
    return json_path, md_path


def _markdown(report: RunReport) -> str:
    return "\n".join(
        [
            f"# WarpTest Report: {report.benchmark}",
            "",
            f"- Spec: `{report.spec_path}`",
            f"- Repo: `{report.repo_path}`",
            f"- Profile: `{report.profile}`",
            f"- App URL: `{report.app_url}`",
            f"- Strategy: `{report.checkpoint_strategy}`",
            f"- Checkpoint validity: `{report.checkpoint_validity_class}`",
            f"- Validity explanation: {report.validity_explanation}",
            f"- Overall: `{report.overall_status}`",
            f"- Target validated before smoke: `{report.target_validated_before_smoke}`",
            "",
            "## Phases",
            f"- Synthesis: `{report.checkpoint_synthesis.status}` {report.checkpoint_synthesis.detail}",
            f"- Restore/Inject: `{report.checkpoint_restoration.status}` {report.checkpoint_restoration.detail}",
            f"- Validation: `{report.validation.status}` {report.validation.detail}",
            f"- Memory/Persistence diff: `{report.memory_persistence_diff}`",
            f"- Soft-lock signal: `{report.soft_lock_signal}`",
            f"- Render readiness signal: `{report.render_readiness_signal}`",
            f"- Post-checkpoint interaction: `{report.post_checkpoint_interaction}`",
            f"- Smoke: `{report.smoke_execution.status}` {report.smoke_execution.detail}",
            f"- Oracle: `{report.oracle_result.status}` {report.oracle_result.detail}",
            f"- Visual Oracle: `{report.visual_oracle_result.status}` {report.visual_oracle_result.detail}",
            "",
            "## Runtime",
            f"- Launch: `{report.launch_command}`",
            f"- Adapters: `{', '.join(report.adapter_layers_used)}`",
            f"- Hook: `{report.hook_availability}`",
            f"- Storage keys touched: `{report.storage_keys_touched}`",
            f"- Console errors: `{len(report.console_errors)}`",
            f"- Network/API: `{report.network_api_calls}`",
            f"- Screenshots: `{report.screenshots}`",
        ]
    ) + "\n"
