"""Read-only evidence collection; never impersonates an executable run identity."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
import json
from pathlib import Path

import yaml

from .lazarus_paper_catalog import PAPER_TASK_IDS, PAPER_CATALOG_PATH
from .lazarus_p2p_only_experiment import (
    FORMAL_MODELS, MANIFEST_PATH, aggregate_p2p_only_reports,
    build_p2p_only_plan, validate_p2p_only_manifest,
)
from .lazarus_p2p_only_identity import canonical_sha256

SCHEMA = "c1-lazarus-p2p-only-collection-v1"
FIELDS = ("task_id", "model", "seed", "arm", "version")


def key(row):
    return tuple(row[field] for field in FIELDS)


def digest(path):
    with Path(path).open("rb") as stream:
        return sha256_stream(stream)


def sha256_stream(stream):
    h = sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        h.update(chunk)
    return h.hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def select_rows(sources, expected):
    """Reject duplicates even if their payloads agree; source choice is explicit."""
    selected = {}
    for source, report in sources:
        for row in report["reports"]:
            identity = key(row)
            if identity not in expected:
                continue
            if identity in selected:
                raise ValueError(f"duplicate collection cell: {identity}")
            if row.get("complete") is not True:
                raise ValueError(f"incomplete collection cell: {identity}")
            selected[identity] = (source, row)
    if set(selected) != expected:
        raise ValueError(f"missing collection cells: {sorted(expected - set(selected))}")
    return selected


def build_collection(workspace, sources, preflights):
    workspace = Path(workspace).resolve()
    manifest_path = workspace / MANIFEST_PATH
    manifest = yaml.safe_load(manifest_path.read_text())
    contract = validate_p2p_only_manifest(
        manifest, catalog_path=workspace / PAPER_CATALOG_PATH, task_ids=PAPER_TASK_IDS,
    )
    plan = build_p2p_only_plan(PAPER_TASK_IDS, models=contract.models, seeds=contract.seeds)
    cells = [dict(zip(FIELDS, (k.task_id, k.model, k.seed, k.arm, k.version))) for k in plan.keys]
    expected = {key(row) for row in cells}
    loaded = [(str(Path(p).resolve()), read(p)) for p in sources]
    selected = select_rows(loaded, expected)
    failures, evidence, source_records, mappings = [], {}, [], []
    ledger_paths = set()
    snapshots = {}

    def check_file(path, expected_sha=None):
        path = str(Path(path).resolve())
        if path not in evidence:
            if not Path(path).is_file():
                failures.append(f"missing evidence: {path}")
                return None
            evidence[path] = digest(path)
        if expected_sha and evidence[path] != expected_sha:
            failures.append(f"evidence SHA mismatch: {path}")
        return evidence[path]

    for source, payload in loaded:
        index_path = Path(source).with_name("run-index.json")
        index = read(index_path)
        for group in index["groups"]:
            for cell in group["cells"]:
                if key(cell["identity"]) in expected:
                    snapshots[(source, key(cell["identity"]))] = cell
        source_records.append({
            "path": source, "sha256": check_file(source),
            "index_path": str(index_path), "index_sha256": check_file(index_path),
            "identity": payload["identity"],
            "formal": payload.get("formal"), "confirmatory": payload.get("confirmatory"),
            "paper_ready": payload.get("paper_ready"),
            "selected_cells": sum(s == source for s, _ in selected.values()),
            "method_protocol": payload.get("method_protocol"),
            "protocol_change": payload.get("protocol_change"),
        })

    def walk_files(node):
        if isinstance(node, dict):
            for name, value in node.items():
                if name in {"live_screenshot", "golden_image", "initial_screenshot"} and value:
                    check_file(value)
                elif name == "cost_ledger" and value and node.get("artifact_dir"):
                    path = Path(node["artifact_dir"]) / value
                    check_file(path)
                    ledger_paths.add(str(path.resolve()))
                elif isinstance(value, (dict, list)):
                    walk_files(value)
            equality = node.get("normalized_image_equality") or {}
            for label, field in (("live", "live_screenshot"), ("golden", "golden_image")):
                if node.get(field) and equality.get(label, {}).get("raw_sha256"):
                    check_file(node[field], equality[label]["raw_sha256"])
        elif isinstance(node, list):
            for value in node:
                walk_files(value)

    rows = []
    for cell in cells:
        identity = key(cell)
        source, row = selected[identity]
        snapshot = snapshots.get((source, identity))
        if snapshot is None:
            failures.append(f"missing snapshot: {identity}")
        else:
            check_file(snapshot["path"], snapshot["sha256"])
            if read(snapshot["path"]) != row:
                failures.append(f"snapshot payload differs: {identity}")
        ledger = row.get("actual_provider_ledger")
        if not ledger:
            failures.append(f"missing provider ledger: {identity}")
        else:
            check_file(ledger)
            ledger_paths.add(str(Path(ledger).resolve()))
            if key(read(ledger)["key"]) != identity:
                failures.append(f"ledger identity mismatch: {identity}")
        walk_files(row)
        runtime = row.get("runtime", {})
        if runtime.get("initial_screenshot"):
            check_file(runtime["initial_screenshot"], runtime.get("initial_screenshot_sha256"))
        synthesis = runtime.get("trace", {}).get("synthesis") or {}
        generated = synthesis.get("generated_spec")
        if generated is not None and canonical_sha256(generated) != row.get("checkpoint_sha256"):
            failures.append(f"generated checkpoint hash mismatch: {identity}")
        if row["arm"] not in {"lazarus_agent_s", "lazarus_scalecua"} and row["version"] == "fixed":
            buggy = selected[(*identity[:-1], "buggy")][1]
            if (not row.get("checkpoint_sha256")
                    or row["checkpoint_sha256"] != buggy.get("checkpoint_sha256")
                    or row.get("checkpoint_reuse_success") is not True
                    or row.get("actual_synthesis_calls") != 0):
                failures.append(f"checkpoint reuse mismatch: {identity}")
        rows.append(row)
        mappings.append({**cell, "source_report": source,
                         "snapshot": snapshot, "provider_ledger": ledger})

    profiles = {}
    for path in preflights:
        check_file(path)
        for probe in read(path).get("models", []):
            model = probe["model"]
            if model not in FORMAL_MODELS:
                continue
            overrides = ({"reasoning_effort": "none"} if model.startswith("gpt-") else
                         {"extra_body": {"thinking": {"type": "disabled"}}})
            if probe.get("thinking_disabled_verified") is not True or probe.get("request_overrides") != overrides:
                failures.append(f"no-thinking preflight invalid: {model}")
            profiles[model] = {k: probe.get(k) for k in (
                "complete", "thinking_disabled_verified", "request_overrides",
                "reasoning_tokens", "reasoning_accounting_anomaly",
            )}
            profiles[model]["source"] = str(Path(path).resolve())
    if set(profiles) != set(FORMAL_MODELS):
        failures.append("no-thinking preflight model coverage incomplete")

    calls, duplicate_calls = {}, 0
    for path in sorted(ledger_paths):
        ledger = read(path)
        usage = ledger.get("usage", ledger)
        for position, call in enumerate(usage.get("calls", [])):
            call_id = call.get("provider_response_id_sha256") or f"{path}#{position}"
            if call_id in calls:
                duplicate_calls += 1
                prior = calls[call_id]
                if any(prior.get(k) != call.get(k) for k in ("model", "prompt_tokens", "completion_tokens", "total_tokens")):
                    failures.append(f"duplicate provider response usage drift: {call_id}")
                continue
            calls[call_id] = {**{k: call.get(k) for k in (
                "model", "kind", "prompt_tokens", "completion_tokens", "total_tokens",
                "reasoning_tokens", "reasoning_expected", "request_contract_passed",
                "reasoning_content",
            )}, "ledger": path, "position": position}
    anomalies = Counter()
    for call in calls.values():
        model = call["model"]
        if f"{model}@relay" not in FORMAL_MODELS:
            continue
        if call["reasoning_expected"] is not False:
            failures.append(f"no-thinking execution expectation missing: {call['ledger']}#{call['position']}")
        if call.get("reasoning_content"):
            failures.append(f"unexpected reasoning content: {model}")
        tokens = call["reasoning_tokens"]
        if model == "kimi-k2.6" and tokens == 1 and not call.get("reasoning_content"):
            anomalies[model] += 1
        elif tokens not in (0, None):
            failures.append(f"unexpected reasoning token count: {model} {tokens}")
    by_model = {}
    for model in sorted({c["model"] for c in calls.values()}):
        subset = [c for c in calls.values() if c["model"] == model]
        known = [c["total_tokens"] for c in subset if isinstance(c["total_tokens"], (int, float))]
        reasoning = [c["reasoning_tokens"] for c in subset if isinstance(c["reasoning_tokens"], (int, float))]
        by_model[model] = {
            "llm_calls": len(subset), "known_total_tokens": sum(known),
            "unknown_total_calls": len(subset) - len(known),
            "observed_reasoning_tokens": sum(reasoning),
            "unknown_reasoning_calls": len(subset) - len(reasoning),
            "reasoning_expected_false_calls": sum(c["reasoning_expected"] is False for c in subset),
            "request_contract_passed_calls": sum(c["request_contract_passed"] is True for c in subset),
        }
    usage = {
        "basis": "selected cell ledgers plus referenced shared synthesis/phase ledgers, unique provider response IDs; fallback ledger path and call offset",
        "known_total_tokens": sum(v["known_total_tokens"] for v in by_model.values()),
        "unknown_total_calls": sum(v["unknown_total_calls"] for v in by_model.values()),
        "llm_calls": len(calls), "duplicate_call_references_removed": duplicate_calls,
        "by_model": by_model, "ledger_paths": sorted(ledger_paths),
        "historical_unattributed_overhead_tokens": None,
        "overhead_note": "Historical preflight, failed attempts and unreferenced shared judging are not allocated to seed 0; treatment attribution is reported separately in aggregate.",
        "new_paid_calls": 0, "new_paid_tokens": 0,
    }
    aggregate = aggregate_p2p_only_reports(rows, plan=plan)
    failures.extend(aggregate["issues"])
    identity_fields = sorted(set().union(*(r["identity"] for r in source_records)))
    differences = {field: [r["identity"].get(field) for r in source_records]
                   for field in identity_fields
                   if len({json.dumps(r["identity"].get(field), sort_keys=True) for r in source_records}) > 1}
    return {
        "schema": SCHEMA, "complete": not failures,
        "formal": False, "confirmatory": False, "paper_ready": False,
        "frozen_specification": {"path": str(manifest_path), "sha256": check_file(manifest_path), "formal": True},
        "expected_cells": len(expected), "observed_cells": len(rows),
        "plan": {"cells": cells}, "reports": rows, "sources": source_records,
        "source_mapping": mappings, "identity_differences": differences,
        "method_protocol": manifest["method_protocol"],
        "h2": "not_applicable_all_tasks_at_goal", "aggregate": aggregate,
        "usage": usage, "no_thinking": {"preflights": profiles, "actual_call_evidence": by_model, "one_token_sentinel_calls": dict(anomalies),
            "limitation": "Historical calls do not all retain wire request overrides; preflight overrides, recorded reasoning expectation and returned reasoning counts are distinguished."},
        "audit": {"complete": not failures, "failures": failures,
                  "evidence_files": evidence, "reused_cells": len(rows), "rerun_cells": 0},
    }
