#!/usr/bin/env python3
"""Read-only recovery audit and a derived 244-row, two-source cost ledger."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from warptest import cost  # noqa: E402
from warptest.c1_synthesis_cache import canonical_hash  # noqa: E402
from warptest.p5_aggregate import _normalize_c1_trial  # noqa: E402
from warptest.c1_state_metrics import explicit_state_stage_metric_issues  # noqa: E402
from warptest.synthesis import parse_llm_checkpoint_output  # noqa: E402
from warptest.synthesis_ablation_study import (  # noqa: E402
    compact_cost, expected_repeated_action_labels, verified_semantic_short_circuit,
)
import yaml  # noqa: E402


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def audit(source, recovery, output):
    old = read(source / "terra-audit.json")
    registration = read(recovery / "recovery-registration.json")
    completion = read(recovery / "execution-completed.json")
    report_path = Path(completion["raw_report"])
    assert digest(report_path) == completion["raw_report_sha256"]
    assert completion["new_synthesis_calls"] == 0
    report = read(report_path)
    assert report["complete"] is True
    assert old["data_integrity_complete"] and len(old["rows"]) == 244
    candidates = {c["task_id"]: c for c in registration["candidates"]}
    assert set(candidates) == {v["task_id"] for v in old["protocol_violations"]}
    assert len(candidates) == 4 and len(report["results"]) == 4
    assert set(candidates) == {r["task_id"] for r in report["results"]}
    assert digest(recovery / "controller/replay_unity_ablation.py") == registration["controller_sha256"]
    replacements, details, warnings = {}, [], []
    for result in report["results"]:
        task_id = result["task_id"]
        candidate = candidates[task_id]
        assert result["seed"] == 0 and result["synthesis_ablation"] == "no_assertion_prediction"
        assert result["model"] == "gpt-5.6-terra"
        assert not result["incomplete"] and not cost.contains_unknown_usage(result)
        assert result["runtime_started"] is True
        assert result["synthesis_actual_cost"]["llm_calls"] == 0
        assert all(c["kind"] == "visual_judge" for c in result["cost"].get("calls", []))
        for name in ("candidate_path", "copied_response"):
            assert digest(candidate[name]) == candidate["candidate_sha256"]
        assert digest(candidate["original_report"]) == candidate["original_report_sha256"]
        original = read(candidate["original_report"])["results"][0]
        spec = parse_llm_checkpoint_output(Path(candidate["candidate_path"]).read_text())["generated_spec"]
        executed_spec = yaml.safe_load(Path(result["config_path"]).read_text())
        assert executed_spec == {**spec, "experiment_seed": 0}
        assert canonical_hash(executed_spec) == result["generated_spec_hash"]
        assert digest(Path(registration["runtime"]) / candidate["oracle_spec"]) == candidate["oracle_sha256"]
        _, errors = _normalize_c1_trial("c1_state", report, result)
        duplicate = "Unity State final semantic checks contain duplicates"
        if duplicate in errors and expected_repeated_action_labels(result, "final"):
            errors.remove(duplicate)
            warnings.append(f"{task_id}: declared repeated-action multiplicity verified")
        stub = "pair_verdict requires visual_grading_started=true"
        if stub in errors and verified_semantic_short_circuit(result):
            errors.remove(stub)
            warnings.append(f"{task_id}: semantic Start short-circuit verified")
        errors.extend(explicit_state_stage_metric_issues(result))
        assert not errors, (task_id, errors)
        assert original["cost"] == original["synthesis_actual_cost"]
        assert original["actual_cost"] == original["cost"]
        assert result["actual_cost"] == result["cost"]
        merged = cost.merge_summaries([original["cost"], result["cost"]])
        for key in ("prompt_tokens", "completion_tokens", "total_tokens", "llm_calls"):
            assert merged[key] == original["cost"][key] + result["cost"][key]
        details.append({
            "task_id": task_id, "candidate_sha256": candidate["candidate_sha256"],
            "original_report": candidate["original_report"],
            "original_report_sha256": candidate["original_report_sha256"],
            "continuation_report": str(report_path), "continuation_report_sha256": digest(report_path),
            "original_synthesis_tokens": original["cost"]["total_tokens"],
            "continuation_tokens": result["cost"]["total_tokens"],
            "combined_tokens": merged["total_tokens"],
            "continuation_llm_calls": result["cost"]["llm_calls"],
            "new_synthesis_calls": 0, "start_success": result["start_success"],
            "e2e_success": result["e2e_success"], "reason": result.get("reason"),
            "original_wall_ms": original["trial_wall_clock_ms"],
            "continuation_wall_ms": result["trial_wall_clock_ms"],
            "timing_kind": "sum_of_original_and_continuation_active_wall_times",
        })
        replacements[task_id] = (result, merged, details[-1])
    rows = []
    for row in old["rows"]:
        assert digest(row["report"]) == row["report_sha256"]
        if row["task_id"] not in replacements or row["variant"] != "no_assertion_prediction":
            rows.append(copy.deepcopy(row))
            continue
        result, merged, lineage = replacements[row["task_id"]]
        corrected = copy.deepcopy(row)
        corrected.update({k: result.get(k) for k in ("start_success", "goal_success", "e2e_success", "status", "reason")})
        corrected.update(
            cost=compact_cost(merged), actual_cost=compact_cost(merged),
            time_ms=row["time_ms"] + result["trial_wall_clock_ms"],
            method_time_ms=row["method_time_ms"] + result["time_e2e_including_synthesis_ms"],
            protocol_issue=None, report=str(report_path), report_sha256=digest(report_path),
            recovery=lineage,
        )
        rows.append(corrected)
    assert sum(a != b for a, b in zip(rows, old["rows"], strict=True)) == 4
    assert len({(r["task_id"], r["model"], r["variant"], r["seed"]) for r in rows}) == 244
    assert not any(r.get("protocol_issue") for r in rows)
    audit_info = {
        "complete": True, "recovered_candidates": details, "retained_unchanged_trials": 240,
        "original_audit": str(source / "terra-audit.json"), "original_audit_sha256": digest(source / "terra-audit.json"),
        "recovery_registration": str(recovery / "recovery-registration.json"),
        "recovery_registration_sha256": digest(recovery / "recovery-registration.json"),
        "warnings": warnings, "auditor_sha256": digest(__file__),
        "cost_attribution": "original generation exactly once plus continuation; no imputation",
        "timing_caveat": "Four entries sum two active attempts; not uninterrupted wall time. Scheduling is serial for recovery.",
    }
    write(output / "recovery-audit.json", audit_info)
    write(output / "registration.json", read(source / "registration.json"))
    write(output / "terra-audit.json", {
        **old, "complete": True, "data_integrity_complete": True,
        "protocol_conformance_complete": True, "protocol_violations": [], "errors": [],
        "warnings": old["warnings"] + warnings, "rows": rows,
        "recovery_audit": str(output / "recovery-audit.json"),
        "recovery_audit_sha256": digest(output / "recovery-audit.json"),
    })
    return audit_info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("source", "recovery", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.source.resolve(), args.recovery.resolve(), args.output.resolve())
    print(json.dumps({"complete": result["complete"], "recovered": len(result["recovered_candidates"]), "retained": 240}))


if __name__ == "__main__":
    main()
