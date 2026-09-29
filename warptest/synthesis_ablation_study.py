"""Register and run the seed-0, Terra-first component ablation study.

Uses the existing C1 execution/grading path. Four rounds counterbalance variant
order by task, while each producer keeps the standard four runtime lanes.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from . import cost
from .c1_provenance import c1_harness_hash
from .c1_state_metrics import explicit_state_stage_metric_issues
from .synthesis_ablation import ABLATION_REVISION, ABLATION_VARIANTS

TERRA = "gpt-5.6-terra@relay"
EXPANSION = ("gemini-3.5-flash@relay", "kimi-k2.6@relay", "gpt-6-astra@relay")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def study_plan(root: Path) -> dict[str, Any]:
    task_file = root / "benchmarks/p5/c1_tasks.yaml"
    tasks = yaml.safe_load(task_file.read_text())["tasks"]
    ids = [task["id"] for task in tasks]
    if len(ids) != 61 or len(set(ids)) != 61:
        raise ValueError("Ablation study requires the frozen 61-task performance catalog")
    batches = [
        {"round": round_index, "variant": variant,
         "task_ids": [task_id for i, task_id in enumerate(ids) if (i + round_index) % 4 == v]}
        for round_index in range(4)
        for v, variant in enumerate(ABLATION_VARIANTS)
    ]
    return {
        "schema_version": 1, "experiment": ABLATION_REVISION,
        "task_file_sha256": hashlib.sha256(task_file.read_bytes()).hexdigest(),
        "task_ids": ids, "seeds": [0], "variants": list(ABLATION_VARIANTS),
        "stages": [
            {"id": "terra", "models": [TERRA], "trial_count": 244},
            {"id": "expansion", "models": list(EXPANSION), "trial_count": 732},
        ],
        "total_trial_count": 976, "batches": batches,
        "order_rule": "task ordinal i receives variant (i + round) mod 4 in round 0..3",
        "workers": {"outer": 8, "inner": 4},
        "expansion_rule": "complete, protocol-correct, usage-audited Terra data; no effect-size or significance threshold",
        "strategy_selection": {"eligible_tasks": 0, "status": "not_applicable"},
    }


def prepare(root: Path, output: Path, credential_source: Path) -> dict[str, Any]:
    plan = study_plan(root)
    source = root / "benchmarks/p5/manifest.yaml"
    base = yaml.safe_load(source.read_text())
    plan["source_manifest_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
    plan_path = output / "registration.json"
    if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
        raise ValueError("Refusing to change an existing study registration")
    for variant in ABLATION_VARIANTS:
        manifest = copy.deepcopy(base)
        manifest["name"] = f"synthesis-ablation-{variant}"
        manifest["description"] = f"StatePortal synthesis ablation: {variant}; seed 0 only"
        manifest["models"] = [TERRA, *EXPANSION]
        manifest["seeds"] = 1
        manifest["seed_base"] = 0
        manifest["arms"] = {"c1_state": manifest["arms"]["c1_state"]}
        manifest["arms"]["c1_state"]["synthesis_ablation"] = variant
        manifest["api"]["credentials_file"] = str(credential_source.resolve())
        manifest["synthesis_ablation_study"] = {
            "revision": ABLATION_REVISION, "variant": variant,
            "seeds": [0], "strategy_selection": "not_applicable",
        }
        path = output / "manifests" / f"{variant}.yaml"
        content = yaml.safe_dump(manifest, sort_keys=False)
        if path.exists() and path.read_text() != content:
            raise ValueError(f"Refusing to overwrite changed manifest: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    write_json(plan_path, plan)
    return plan


def batch_directory(output: Path, stage: str, batch: dict[str, Any]) -> Path:
    return output / stage / f"round-{batch['round']}" / batch["variant"]


def index_rows(index: dict[str, Any]) -> list[dict[str, Any]]:
    """Read the actual P5 index envelope, not an invented flat arm mapping."""
    arms = index.get("arms", {})
    if not isinstance(arms, dict) or not isinstance(arms.get("c1_state", []), list):
        raise ValueError("Invalid P5 arm index")
    return arms.get("c1_state", [])


def study_sources(output: Path) -> list[Path]:
    receipt = output / "retained-runs.json"
    retained = json.loads(receipt.read_text())["roots"] if receipt.exists() else []
    return [*[Path(path) for path in retained], output]


def completed_batch_entries(output: Path, stage: dict[str, Any], batch: dict[str, Any]):
    """Yield every immutable completed treatment, including semantic failures."""
    for source in study_sources(output):
        directory = batch_directory(source, stage["id"], batch)
        path = directory / "p5-run-index.json"
        if not path.exists():
            continue
        index = json.loads(path.read_text())
        for entry in index_rows(index):
            if entry.get("status") == "ok":
                yield directory, index, entry


def validate_retained_runs(output: Path, retained: list[Path], harness_hash: str) -> None:
    roots = [str(path.resolve()) for path in retained]
    if len(roots) != len(set(roots)) or str(output.resolve()) in roots:
        raise ValueError("Duplicate or recursive retained run roots")
    receipt_path = output / "retained-runs.json"
    evidence = []
    for root in retained:
        freeze = json.loads((root / "harness-freeze.json").read_text())
        if freeze["harness_hash"] != harness_hash:
            raise ValueError("Retained treatment harness differs; no automatic migration")
        for path in root.glob("*/round-*/*/p5-run-index.json"):
            raw = path.read_bytes()
            index = json.loads(raw)
            if index.get("harness_hash") != harness_hash:
                raise ValueError(f"Retained index harness mismatch: {path}")
            for entry in index_rows(index):
                if entry.get("status") == "pending":
                    continue
                if entry.get("status") != "ok":
                    # Only the identified, zero-call presentation admission
                    # failure is safe to replace automatically. Other failed
                    # paid attempts need their registered recovery audit.
                    raw_path = entry.get("report")
                    if not raw_path:
                        raise ValueError("Noncompleted source has no auditable zero-call report")
                    failed_raw = Path(raw_path).read_bytes()
                    if hashlib.sha256(failed_raw).hexdigest() != entry.get("report_sha256"):
                        raise ValueError("Changed noncompleted source report")
                    failed = next(r for r in json.loads(failed_raw)["results"] if r["task_id"] == entry["task_id"])
                    ledger = failed.get("actual_cost") or {}
                    safe = (failed.get("reason") in {
                        "LibreOffice C1 State requires a headed native desktop session",
                        "Blender C1 State requires a headed native desktop session",
                    } and ledger.get("llm_calls") == 0 and ledger.get("total_tokens") == 0
                        and ledger.get("usage_complete") is True and not cost.contains_unknown_usage(failed))
                    if not safe:
                        raise ValueError("Noncompleted paid source needs a separate recovery audit")
                    continue
                report_path = Path(entry["report"])
                report_raw = report_path.read_bytes()
                if hashlib.sha256(report_raw).hexdigest() != entry.get("report_sha256"):
                    raise ValueError(f"Changed retained report: {report_path}")
                result = next(r for r in json.loads(report_raw)["results"] if r["task_id"] == entry["task_id"])
                # The interrupted controller used headless=True. Browser tasks
                # are forced offscreen under both settings; these two native
                # backends do not consume that flag in their treatment path.
                unchanged = {"2048", "datingsim", "spacerpg", "jupyterlab", "luanti", "openra"}
                if index.get("headless") is True and str(result.get("benchmark")).lower() not in unchanged:
                    raise ValueError(f"Headed-mode equivalence not established for {entry['task_id']}")
                if result.get("incomplete") or cost.contains_unknown_usage(result):
                    raise ValueError("Cannot retain incomplete or unknown-usage treatment")
            evidence.append({"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()})
    receipt = {"roots": roots, "harness_hash": harness_hash, "source_indices": evidence,
               "policy": "retain all completed treatments, including model failures; execute only missing tuples",
               "presentation_fix": "headless=False permits native desktop backends; background browsers remain headless"}
    if receipt_path.exists() and json.loads(receipt_path.read_text()) != receipt:
        raise ValueError("Retained evidence changed since registration")
    write_json(receipt_path, receipt)


def expected_repeated_action_labels(result: dict[str, Any], label: str) -> bool:
    """Distinguish repeated transition actions from duplicated oracle checks.

    Unity labels action receipts by type rather than ordinal. Two declared
    level-up actions therefore legitimately have the same receipt name. Only
    this exact case is accepted; duplicated assertions/Start checks still fail.
    """
    if label != "final":
        return False
    observation = ((result.get("seed_evidence") or {}).get(label) or {}).get("semantic_observation") or {}
    checks = observation.get("checks")
    if not isinstance(checks, list) or not all(isinstance(c, dict) and isinstance(c.get("name"), str) for c in checks):
        return False
    names = Counter(c["name"] for c in checks)
    duplicated = {name for name, n in names.items() if n > 1}
    if not duplicated or any(not name.startswith("action[") for name in duplicated):
        return False
    try:
        spec = yaml.safe_load(Path(result["config_path"]).read_text())
        from .c1_synthesis_cache import canonical_hash
        if canonical_hash(spec) != result.get("generated_spec_hash"):
            return False
        declared = Counter(a["type"] for a in spec["actions"])
    except (OSError, KeyError, TypeError, yaml.YAMLError):
        return False
    observed = Counter()
    repeated_types = set()
    for name, count in names.items():
        match = re.fullmatch(r"action\[([^\]]+)\](?:\..+)?", name)
        if name in duplicated and match is None:
            return False
        if match:
            observed[match.group(1)] += count
            if name in duplicated:
                repeated_types.add(match.group(1))
    return bool(repeated_types) and all(observed[kind] == declared[kind] for kind in repeated_types)


def verified_semantic_short_circuit(result: dict[str, Any]) -> bool:
    """Recognize the producer's explicit, zero-judge-call failed-Start stub.

    A structured not_evaluated pair verdict records a short circuit, not a
    visual judgment. Require the exact stub, all failed/skipped stage flags,
    and a complete call ledger containing synthesis calls only.
    """
    expected_pair = {
        "result": False, "status": "failure",
        "before": {"stage": "before", "label": "not_evaluated",
                   "evaluation_status": "not_evaluated_semantic_failed"},
        "after": {"stage": "after", "label": "not_evaluated",
                  "evaluation_status": "not_evaluated_start_failed"},
        "after_evaluated": False,
    }
    if result.get("pair_verdict") != expected_pair:
        return False
    if any(result.get(key) is not False for key in (
        "result", "visual_grading_started", "before_semantic_ok", "before_visual_ok",
        "after_semantic_ok", "after_visual_ok", "start_success", "goal_success", "e2e_success",
    )):
        return False
    if result.get("start_grading_started") is not True:
        return False
    expected_status = {
        "start_gate_status": "semantic_failed",
        "goal_evaluation_status": "not_evaluated_start_failed",
        "before_visual_status": "not_evaluated_semantic_failed",
        "after_semantic_status": "not_evaluated", "after_visual_status": "not_evaluated",
    }
    if any(result.get(key) != value for key, value in expected_status.items()):
        return False
    if result.get("before_semantic_status") not in {"failure", "validate_failed", "settings_mismatch"}:
        return False
    ledger = result.get("actual_cost")
    if not isinstance(ledger, dict) or ledger.get("usage_complete") is not True:
        return False
    calls = ledger.get("calls")
    allowed = {"checkpoint_exploration", "checkpoint_synthesis", "checkpoint_synthesis_repair"}
    return (isinstance(calls, list) and bool(calls) and len(calls) == ledger.get("llm_calls")
            and all(isinstance(call, dict) and call.get("kind") in allowed
                    and call.get("usage_complete") is True for call in calls)
            and not cost.contains_unknown_usage(ledger))


def compact_cost(ledger: Any) -> Any:
    """Keep audited cost totals, leaving raw prompts/responses in source reports."""
    return {key: value for key, value in ledger.items() if key != "calls"} if isinstance(ledger, dict) else ledger


def audit_stage(output: Path, stage: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    from .p5_aggregate import _normalize_c1_trial

    expected = {(task, model, variant) for task in plan["task_ids"]
                for model in stage["models"] for variant in ABLATION_VARIANTS}
    seen: set[tuple[str, str, str]] = set()
    rows = []
    errors = []
    warnings = []
    protocol_violations = []
    for batch in plan["batches"]:
        for directory, index, entry in completed_batch_entries(output, stage, batch):
            key = (str(entry.get("task_id")), str(entry.get("model")), batch["variant"])
            if key not in expected or key in seen or entry.get("seed") != 0:
                errors.append(f"Unexpected/duplicate tuple: {key}")
                continue
            if key[0] not in batch["task_ids"]:
                errors.append(f"Task assigned to wrong batch: {key}")
                continue
            seen.add(key)
            if entry.get("status") != "ok" or not entry.get("report"):
                errors.append(f"Unfinished or infrastructure-failed tuple: {key}")
                continue
            path = Path(entry["report"])
            if not path.is_absolute():
                path = directory / path
            if not path.is_file():
                errors.append(f"Missing report: {key}")
                continue
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != entry.get("report_sha256"):
                errors.append(f"Report hash mismatch: {key}")
                continue
            report = json.loads(raw)
            candidates = [r for r in report.get("results", []) if r.get("task_id") == key[0]]
            if len(candidates) != 1:
                errors.append(f"Non-unique task record: {key}")
                continue
            result = candidates[0]
            protocol_issue = None
            if (batch["variant"] == "no_assertion_prediction"
                    and "generated_spec assertions must be a non-empty list" in str(result.get("reason") or "")):
                protocol_issue = "Unity shared acceptance gate rejects the empty assertions required by this ablation"
                protocol_violations.append({"task_id": key[0], "model": key[1], "variant": key[2],
                                            "issue": protocol_issue, "report": str(path),
                                            "report_sha256": entry["report_sha256"]})
            if result.get("synthesis_ablation") != batch["variant"]:
                errors.append(f"Treatment identity mismatch: {key}")
            if result.get("incomplete") or cost.contains_unknown_usage(result):
                errors.append(f"Incomplete data or unknown usage: {key}")
            for ledger_name in ("cost", "actual_cost"):
                ledger = result.get(ledger_name)
                if not isinstance(ledger, dict) or any(
                    type(ledger.get(field)) is not int or ledger[field] < 0
                    for field in ("prompt_tokens", "completion_tokens", "total_tokens")
                ):
                    errors.append(f"Missing primary usage in {ledger_name}: {key}")
                elif ledger["prompt_tokens"] + ledger["completion_tokens"] != ledger["total_tokens"]:
                    errors.append(f"Primary token equation mismatch in {ledger_name}: {key}")
            _, normalization_errors = _normalize_c1_trial("c1_state", report, result)
            # Existing browser/Luanti producers emit the complete explicit
            # Start/Goal contract but omit this auxiliary lifecycle flag. Do
            # not invent it or rewrite raw evidence. The study never uses it
            # as a metric, and still validates every stage boolean below.
            optional_flag_issue = "state runtime_started must be an explicit boolean"
            if "runtime_started" not in result and optional_flag_issue in normalization_errors:
                normalization_errors.remove(optional_flag_issue)
                warnings.append(f"{key}: optional runtime_started metadata absent; original record retained")
            repeated_action_issue = "Unity State final semantic checks contain duplicates"
            if repeated_action_issue in normalization_errors and expected_repeated_action_labels(result, "final"):
                normalization_errors.remove(repeated_action_issue)
                warnings.append(f"{key}: repeated action receipt labels match the declared action multiplicity; oracle assertion labels remain unique")
            short_circuit_issue = "pair_verdict requires visual_grading_started=true"
            if short_circuit_issue in normalization_errors and verified_semantic_short_circuit(result):
                normalization_errors.remove(short_circuit_issue)
                warnings.append(f"{key}: exact not_evaluated pair stub, failed Start and zero visual-judge calls verified")
            errors.extend(f"{key}: {error}" for error in normalization_errors)
            errors.extend(f"{key}: {error}" for error in explicit_state_stage_metric_issues(result))
            if type(result.get("trial_wall_clock_ms")) is not int or result["trial_wall_clock_ms"] < 0:
                errors.append(f"Missing trial wall clock: {key}")
            rows.append({"task_id": key[0], "model": key[1], "variant": key[2], "seed": 0,
                         "benchmark": result.get("benchmark"),
                         "start_success": result.get("start_success"),
                         "goal_success": result.get("goal_success"),
                         "e2e_success": result.get("e2e_success"),
                         "cost": compact_cost(result.get("cost")), "actual_cost": compact_cost(result.get("actual_cost")),
                         "time_ms": result.get("trial_wall_clock_ms"),
                         "method_time_ms": result.get("time_e2e_including_synthesis_ms"),
                         "status": result.get("status"),
                         "protocol_issue": protocol_issue,
                         "reason": result.get("reason"), "report": str(path),
                         "report_sha256": entry["report_sha256"]})
        for source in study_sources(output):
            directory = batch_directory(source, stage["id"], batch)
            for meta_path in directory.rglob("synthesis-meta.json"):
                meta = json.loads(meta_path.read_text())
                if meta.get("ablation_variant") != batch["variant"]:
                    errors.append(f"Wrong synthesis variant: {meta_path}")
                if batch["variant"] == "no_source_exploration" and (meta.get("tool_calls") or meta.get("files_read")):
                    errors.append(f"Source access in no-source treatment: {meta_path}")
                if meta.get("prompt_private_reference_count"):
                    errors.append(f"Private reference in synthesis prompt: {meta_path}")
    missing = sorted(expected - seen)
    if missing:
        errors.append(f"Missing {len(missing)} registered tuples")
    data_integrity_complete = not errors and len(rows) == stage["trial_count"]
    if protocol_violations:
        errors.append(f"Intervention protocol mismatch: {len(protocol_violations)} no-assertion candidates were rejected by a nonempty-assertion requirement")
    audit = {"stage": stage["id"], "complete": not errors and len(rows) == stage["trial_count"],
             "data_integrity_complete": data_integrity_complete,
             "protocol_conformance_complete": not protocol_violations,
             "protocol_violations": protocol_violations,
             "expected_trials": stage["trial_count"], "observed_trials": len(rows),
             "errors": errors, "warnings": warnings, "rows": rows,
             "expansion_depends_on_effect_direction": False}
    write_json(output / f"{stage['id']}-audit.json", audit)
    return audit


def execute(
    root: Path, output: Path, credentials: Path, plan: dict[str, Any],
    retained: list[Path] | None = None, *, allow_expansion: bool = False,
) -> None:
    from .c1_experiment import run_c1_preflight
    from .p5_scale import run_p5_scale

    frozen_hash = c1_harness_hash(root)
    validate_retained_runs(output, retained or [], frozen_hash)
    freeze_path = output / "harness-freeze.json"
    if freeze_path.exists() and json.loads(freeze_path.read_text())["harness_hash"] != frozen_hash:
        raise ValueError("Study harness changed; do not mix old and new results")
    write_json(freeze_path, {"root": str(root), "harness_hash": frozen_hash})
    for stage in plan["stages"]:
        if stage["id"] == "expansion":
            if not allow_expansion:
                write_json(output / "awaiting-user-instructions.json", {
                    "status": "terra_complete_waiting_for_user",
                    "authorized_models": [TERRA],
                    "expansion_started": False,
                    "next_action": "Report complete Terra results and await a new user instruction",
                })
                print("Terra complete; expansion is not authorized. Awaiting user instructions.", flush=True)
                return
            audit = audit_stage(output, plan["stages"][0], plan)
            if not audit["complete"]:
                raise RuntimeError("Terra integrity audit failed; expansion remains stopped")
        for batch in plan["batches"]:
            directory = batch_directory(output, stage["id"], batch)
            manifest = output / "manifests" / f"{batch['variant']}.yaml"
            wanted = {(task, model, 0) for task in batch["task_ids"] for model in stage["models"]}
            completed = [(r.get("task_id"), r.get("model"), r.get("seed"))
                         for _, _, r in completed_batch_entries(output, stage, batch)]
            if len(completed) != len(set(completed)) or not set(completed) <= wanted:
                raise ValueError("Retained batch has duplicate or unregistered treatments")
            remaining = wanted - set(completed)
            if not remaining:
                print(f"Retaining completed batch {stage['id']} {batch['round']} {batch['variant']}", flush=True)
                continue
            remaining_tasks = [task for task in batch["task_ids"] if any(item[0] == task for item in remaining)]
            if remaining != {(task, model, 0) for task in remaining_tasks for model in stage["models"]}:
                raise ValueError("Recovery needs separate model subsets; refusing duplicate paid trials")
            if c1_harness_hash(root) != frozen_hash:
                raise RuntimeError("Frozen harness changed before dispatch")
            gate = output / "preflight" / f"{stage['id']}-{batch['variant']}.json"
            # Preflight is per model set and variant manifest. Never rewrite an
            # existing gate; stale gates are rejected by the standard producer.
            if not gate.exists():
                preflight = run_c1_preflight(credentials, root=root, manifest_path=manifest,
                                            decision_models=stage["models"])
                write_json(gate, preflight)
                if preflight.get("complete") is not True:
                    raise RuntimeError(f"Online preflight failed: {gate}")
            index = directory / "p5-run-index.json"
            if index.exists():
                raise RuntimeError(f"Existing incomplete batch requires explicit trial-atomic recovery: {index}")
            print(f"Running {stage['id']} round={batch['round']} variant={batch['variant']} tasks={len(remaining_tasks)} retained={len(completed)}", flush=True)
            run_p5_scale(root, manifest_path=manifest, models=stage["models"], seeds=1,
                         arms=["c1_state"], task_ids=remaining_tasks, report_dir=directory, headless=False,
                         preflight_artifact=gate, c1_outer_worker=8, c1_inner_worker=4)
        audit = audit_stage(output, stage, plan)
        if not audit["complete"]:
            raise RuntimeError(f"Stage integrity audit failed: {output / (stage['id'] + '-audit.json')}")
        print(f"Stage {stage['id']} passed integrity audit: {audit['observed_trials']} trials", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--run", action="store_true", help="Execute paid preflight and the authorized matrix")
    parser.add_argument("--retain-run", action="append", type=Path, default=[])
    parser.add_argument("--allow-expansion", action="store_true",
                        help="Only use after a new explicit user authorization for the other three models")
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    plan = prepare(root, output, args.credentials)
    if args.run:
        execute(root, output, args.credentials.resolve(), plan, args.retain_run,
                allow_expansion=args.allow_expansion)
    else:
        from .p5_scale import run_p5_scale
        dry_runs = {}
        for variant in ABLATION_VARIANTS:
            dry_runs[variant] = run_p5_scale(
                root, manifest_path=output / "manifests" / f"{variant}.yaml",
                models=[TERRA], seeds=1, arms=["c1_state"], dry_run=True,
                c1_outer_worker=8, c1_inner_worker=4,
            )
        write_json(output / "terra-dry-run.json", dry_runs)
        print(json.dumps({"registration": str(output / "registration.json"),
                          "stage1_trials": 244, "expansion_trials": 732, "paid_calls": 0}))


if __name__ == "__main__":
    main()
