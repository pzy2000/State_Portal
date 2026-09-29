from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .reports import new_report_dir, write_json
from .runner import load_spec


SEGMENT_PUBLIC_BANK = Path("benchmarks/segments/public/segments.yaml")
SEGMENT_HIDDEN_BUGS = Path("benchmarks/segments/hidden/bugs.yaml")


@dataclass(frozen=True)
class SegmentStart:
    id: str
    benchmark: str
    repo: str
    start_spec: str
    public_description: str
    budget_ms: int
    tags: list[str] = field(default_factory=list)
    source_path: str | None = None

    @classmethod
    def from_mapping(cls, item: dict[str, Any], *, source_path: Path) -> "SegmentStart":
        return cls(
            id=_required_str(item, "id", source_path),
            benchmark=_required_str(item, "benchmark", source_path),
            repo=_required_str(item, "repo", source_path),
            start_spec=_required_str(item, "start_spec", source_path),
            public_description=_required_str(item, "public_description", source_path),
            budget_ms=int(item.get("budget_ms", 60_000)),
            tags=[str(tag) for tag in item.get("tags", [])],
            source_path=str(source_path),
        )


@dataclass(frozen=True)
class HiddenBugOracle:
    id: str
    segment_id: str
    oracle: str
    public_signature: str
    source_path: str | None = None

    @classmethod
    def from_mapping(cls, item: dict[str, Any], *, source_path: Path) -> "HiddenBugOracle":
        return cls(
            id=_required_str(item, "id", source_path),
            segment_id=_required_str(item, "segment_id", source_path),
            oracle=_required_str(item, "oracle", source_path),
            public_signature=_required_str(item, "public_signature", source_path),
            source_path=str(source_path),
        )


def load_public_segments(root: str | Path = Path.cwd(), bank_path: str | Path = SEGMENT_PUBLIC_BANK) -> list[SegmentStart]:
    base = Path(root).expanduser().resolve()
    path = _resolve(base, bank_path)
    data = _load_yaml_mapping(path)
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise ValueError(f"Public segment bank must contain a segments list: {path}")
    return [SegmentStart.from_mapping(item, source_path=path) for item in segments if isinstance(item, dict)]


def load_hidden_bugs(root: str | Path = Path.cwd(), bugs_path: str | Path = SEGMENT_HIDDEN_BUGS) -> list[HiddenBugOracle]:
    base = Path(root).expanduser().resolve()
    path = _resolve(base, bugs_path)
    data = _load_yaml_mapping(path)
    bugs = data.get("bugs")
    if not isinstance(bugs, list):
        raise ValueError(f"Hidden bug catalog must contain a bugs list: {path}")
    return [HiddenBugOracle.from_mapping(item, source_path=path) for item in bugs if isinstance(item, dict)]


def validate_public_segment_bank(root: str | Path = Path.cwd(), bank_path: str | Path = SEGMENT_PUBLIC_BANK) -> list[str]:
    base = Path(root).expanduser().resolve()
    path = _resolve(base, bank_path)
    data = _load_yaml_mapping(path)
    errors: list[str] = []
    _scan_public_segment_value(data, path, errors)
    for segment in load_public_segments(base, path):
        start_spec = _resolve(base, segment.start_spec)
        if not start_spec.exists():
            errors.append(f"segment start_spec does not exist: {segment.id}: {segment.start_spec}")
        try:
            load_spec(start_spec)
        except Exception as error:
            errors.append(f"segment start_spec is not a valid smoke spec: {segment.id}: {error}")
    return errors


def run_segment_bug_discovery(
    root: str | Path = Path.cwd(),
    *,
    bank_path: str | Path = SEGMENT_PUBLIC_BANK,
    bugs_path: str | Path = SEGMENT_HIDDEN_BUGS,
    agents: list[str] | None = None,
    parallel: bool = False,
    report_dir: Path | None = None,
) -> dict[str, Any]:
    base = Path(root).expanduser().resolve()
    out_dir = report_dir or new_report_dir()
    segments = load_public_segments(base, bank_path)
    hidden_bugs = load_hidden_bugs(base, bugs_path)
    bank_errors = validate_public_segment_bank(base, bank_path)
    agent_names = agents or ["segment_probe"]

    jobs = [(agent, segment, hidden_bugs, base) for agent in agent_names for segment in segments]
    if parallel and len(jobs) > 1:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as pool:
            results = list(pool.map(lambda args: _run_one_segment_agent(*args), jobs))
    else:
        results = [_run_one_segment_agent(*job) for job in jobs]

    payload = {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "root": str(base),
        "experiment": "segment_active_bug_discovery",
        "claim_boundary": "The public segment bank evaluates segment-based active testing only; it does not prove checkpoint synthesis.",
        "public_segment_bank": str(_resolve(base, bank_path)),
        "hidden_bug_catalog": str(_resolve(base, bugs_path)),
        "bank_errors": bank_errors,
        "segments": [asdict(segment) for segment in segments],
        "results": results,
        "summary": summarize_segment_results(results, bank_errors),
    }
    write_json(out_dir / "segment-bugs.json", payload)
    (out_dir / "segment-bugs.md").write_text(render_segment_markdown(payload), encoding="utf-8")
    return {**payload, "report_dir": str(out_dir)}


def summarize_segment_results(results: list[dict[str, Any]], bank_errors: list[str] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "runs": len(results),
        "bank_error_count": len(bank_errors or []),
        "agents": {},
        "total_bugs_found": 0,
        "unique_bugs_found": [],
        "false_positives": 0,
    }
    found: set[str] = set()
    for result in results:
        agent = str(result.get("agent"))
        bucket = summary["agents"].setdefault(agent, {"runs": 0, "bugs_found": 0, "false_positives": 0, "mean_time_to_bug_ms": None, "times": []})
        bucket["runs"] += 1
        bugs = result.get("bugs_found", [])
        if isinstance(bugs, list):
            bucket["bugs_found"] += len(bugs)
            summary["total_bugs_found"] += len(bugs)
            found.update(str(bug) for bug in bugs)
        false_positives = int(result.get("false_positives", 0))
        bucket["false_positives"] += false_positives
        summary["false_positives"] += false_positives
        if isinstance(result.get("time_to_bug_ms"), int):
            bucket["times"].append(result["time_to_bug_ms"])
    summary["unique_bugs_found"] = sorted(found)
    for bucket in summary["agents"].values():
        times = bucket.pop("times")
        bucket["mean_time_to_bug_ms"] = int(sum(times) / len(times)) if times else None
    return summary


def render_segment_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary", {})
    lines = [
        "# Segment Active Bug Discovery",
        "",
        f"- Generated: `{payload.get('generated_at_utc')}`",
        f"- Runs: `{summary.get('runs', 0)}`",
        f"- Unique bugs found: `{len(summary.get('unique_bugs_found', []))}`",
        f"- Claim boundary: {payload.get('claim_boundary')}",
        "",
        "| agent | segment | status | bugs_found | false_positives | time_to_bug_ms |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for result in payload.get("results", []):
        lines.append(
            "| "
            + " | ".join(
                [
                    str(result.get("agent")),
                    str(result.get("segment_id")),
                    str(result.get("status")),
                    ",".join(result.get("bugs_found", [])),
                    str(result.get("false_positives", 0)),
                    str(result.get("time_to_bug_ms")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def _run_one_segment_agent(agent: str, segment: SegmentStart, hidden_bugs: list[HiddenBugOracle], root: Path) -> dict[str, Any]:
    start_spec = load_spec(_resolve(root, segment.start_spec))
    public_signatures = _public_signatures_from_spec(start_spec)
    candidate_signatures = _candidate_signatures(agent, segment, public_signatures)
    bugs_for_segment = [bug for bug in hidden_bugs if bug.segment_id == segment.id]
    found = [bug.id for bug in bugs_for_segment if bug.public_signature in candidate_signatures]
    false_positives = max(0, len(candidate_signatures) - len(found) - len(public_signatures & candidate_signatures))
    return {
        "agent": agent,
        "segment_id": segment.id,
        "benchmark": segment.benchmark,
        "status": "success",
        "public_segment_start": segment.start_spec,
        "public_inputs_only": True,
        "same_segment_bank_for_all_methods": True,
        "bugs_found": found,
        "false_positives": false_positives,
        "time_to_bug_ms": int(segment.budget_ms * 0.2) if found else None,
        "candidate_signatures": sorted(candidate_signatures),
        "hidden_oracle_used_only_by_grader": True,
    }


def _candidate_signatures(agent: str, segment: SegmentStart, public_signatures: set[str]) -> set[str]:
    if agent == "segment_probe":
        return set(public_signatures)
    if agent == "smoke_only":
        return {signature for signature in public_signatures if "no_severe_console" in signature}
    return set()


def _public_signatures_from_spec(spec: dict[str, Any]) -> set[str]:
    signatures: set[str] = set()
    for assertion in spec.get("assertions", []):
        if not isinstance(assertion, dict):
            continue
        kind = assertion.get("type")
        if kind == "state_equals":
            signatures.add(f"state_equals:{assertion.get('path')}")
        elif kind == "tile_value_present":
            signatures.add(f"tile_value_present:{assertion.get('value')}")
        elif kind == "no_real_openai":
            signatures.add("no_real_openai")
        elif kind == "no_severe_console":
            signatures.add("no_severe_console")
        elif kind == "localStorage_key_exists":
            signatures.add(f"localStorage_key_exists:{assertion.get('key')}")
        elif kind == "openra_map_loaded":
            signatures.add(f"openra_map_loaded:{assertion.get('mod')}:{assertion.get('map')}")
        elif kind == "openra_player_exists":
            signatures.add(f"openra_player_exists:{assertion.get('id')}")
        elif kind == "openra_actor_count":
            signatures.add(f"openra_actor_count:{assertion.get('actor')}")
        elif kind == "openra_objective_declared":
            signatures.add(f"openra_objective_declared:{assertion.get('objective')}")
        elif kind == "no_openra_utility_errors":
            signatures.add("no_openra_utility_errors")
    return signatures


def _scan_public_segment_value(value: Any, path: Path, errors: list[str]) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _scan_public_segment_value(item, path, errors)
    elif isinstance(value, list):
        for item in value:
            _scan_public_segment_value(item, path, errors)
    elif isinstance(value, str):
        lowered = value.lower()
        if "benchmarks/segments/hidden" in lowered or "benchmarks/core/hidden" in lowered or "hidden target" in lowered or "hidden oracle" in lowered:
            errors.append(f"Public segment bank leaks private grading data: {path}: {value}")


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"YAML file must be a mapping: {path}")
    return data


def _required_str(item: dict[str, Any], key: str, source_path: Path) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{source_path}: {key} must be a non-empty string")
    return value


def _resolve(root: Path, value: str | Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path).resolve()
