#!/usr/bin/env python3
"""Audit a complete real-start P2P-only run and index."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import sys
from itertools import product

from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from warptest.lazarus_paper_catalog import PAPER_TASK_IDS  # noqa: E402
from warptest.lazarus_p2p_only_experiment import EXPECTED_MODELS, registered_schedule  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root: Path) -> dict[str, Any]:
    if (root / "collection.json").is_file():
        return audit_collection(root)
    run_path = root / "run.json"
    index_path = root / "run-index.json"
    failures: list[str] = []
    coverage_failures: list[str] = []
    run = json.loads(run_path.read_text(encoding="utf-8"))
    index = json.loads(index_path.read_text(encoding="utf-8"))
    readiness = json.loads((root / "readiness.json").read_text(encoding="utf-8"))
    reports = [row for row in run.get("reports") or [] if isinstance(row, dict)]
    expected = {
        (
            str(row["task_id"]),
            str(row["model"]),
            int(row["seed"]),
            str(row["arm"]),
            str(row["version"]),
        )
        for row in readiness.get("plan", {}).get("cells") or []
    }
    observed_rows = [
        (
            str(row.get("task_id")),
            str(row.get("model")),
            int(row.get("seed", -1)),
            str(row.get("arm")),
            str(row.get("version")),
        )
        for row in reports
    ]
    observed = set(observed_rows)
    duplicates = sorted(key for key, count in Counter(observed_rows).items() if count != 1)
    v3 = run.get("schema") == "c1-lazarus-p2p-only-run-v3"
    seeds = {key[2] for key in expected}
    if v3:
        arms = {"lazarus_agent_s", "lazarus_agent_s_synth", "lazarus_state"}
    else:
        arms = {"lazarus_agent_s", "lazarus_state"}
    formal_arms = {
        "lazarus_agent_s",
        "lazarus_agent_s_synth",
        "lazarus_state",
        "lazarus_scalecua",
        "lazarus_scalecua_synth",
    }
    models = tuple(dict.fromkeys(str(row["model"]) for row in readiness.get("plan", {}).get("cells") or []))
    try:
        _profiles, _scope, _auth_id, schedule_arms = registered_schedule(
            models, sorted(seeds)
        )
    except ValueError as error:
        schedule_arms = None
        if v3:
            failures.append(str(error))
    expected_arms = (
        arms
        if v3
        else (schedule_arms if schedule_arms is not None else formal_arms)
    )
    registered = set(product(PAPER_TASK_IDS, models if v3 else EXPECTED_MODELS, seeds, expected_arms, ("buggy", "fixed")))
    if seeds not in (({0}, {0, 1, 2}) if v3 else ({0},)) or expected != registered:
        failures.append("readiness plan differs from a registered complete schedule")
    count = len(expected)
    group_count = len({key[:3] for key in expected})
    if readiness.get("complete") is not True or readiness.get("expected_cells") != count:
        failures.append("readiness is incomplete or has inconsistent cell count")
    if run.get("identity") != readiness.get("identity") or index.get("identity") != readiness.get("identity"):
        failures.append("run/index/readiness identity drift")
    transport = run.get("protocol_change")
    if transport:
        from warptest.image_history_backoff import protocol
        authorization = json.loads((root / "authorization.json").read_text())
        if transport != protocol() or authorization.get("image_history_backoff") != transport:
            failures.append("image history protocol authorization drift")
        for receipt in run.get("image_history_backoff_receipts", []):
            path = Path(str(receipt.get("receipt_path", "")))
            if not path.is_file() or json.loads(path.read_text()) != receipt:
                failures.append("image history backoff receipt drift")
            rejections = receipt.get("rejections", [])
            original = receipt.get("original_image_count", 0)
            sent = receipt.get("sent_image_count", 0)
            if (receipt.get("revision") != transport["revision"]
                    or receipt.get("model") != "kimi-k2.6"
                    or sent < 1 or original - sent != len(rejections)
                    or len(str(receipt.get("latest_image_sha256", ""))) != 64
                    or len(str(receipt.get("all_text_sha256", ""))) != 64):
                failures.append("invalid image history preservation receipt")
            for offset, rejection in enumerate(rejections):
                proof = rejection.get("provider_capacity", {})
                if (proof.get("status_code") != 429
                        or proof.get("request_tokens", 0) <= proof.get("limit_tokens", 0)
                        or proof.get("limit_tokens", 0) <= 0
                        or rejection.get("rejected_image_count") != original - offset
                        or len(str(rejection.get("dropped_image_sha256", ""))) != 64):
                    failures.append("unproven capacity-triggered history reduction")
    if run.get("schema") not in {"c1-lazarus-p2p-only-run-v2", "c1-lazarus-p2p-only-run-v3"}:
        failures.append("unknown run schema")
    if run.get("complete") is not True or len(reports) != count:
        coverage_failures.append(f"run is not complete with {count} reports")
    if expected - observed:
        coverage_failures.append("expected tuple coverage has missing cells")
    if observed - expected or duplicates:
        failures.append("tuple coverage has extra or duplicate cells")
    if any(row.get("complete") is not True for row in reports):
        failures.append("at least one cell is incomplete")
    if any(
        isinstance(row.get("treatment_tokens"), bool)
        or not isinstance(row.get("treatment_tokens"), (int, float))
        or float(row["treatment_tokens"]) < 0
        for row in reports
    ):
        failures.append("primary treatment usage is unknown or invalid")
    by_state: dict[tuple[str, str, int], dict[str, dict[str, Any]]] = {}
    for row in reports:
        synth_arms = (
            {"lazarus_state", "lazarus_agent_s_synth", "lazarus_scalecua_synth"}
            if v3
            else {"lazarus_state"}
        )
        if row.get("arm") in synth_arms:
            key = (str(row["task_id"]), str(row["model"]), int(row["seed"]), str(row["arm"]))
            by_state.setdefault(key, {})[str(row["version"])] = row
    for key, versions in by_state.items():
        if set(versions) != {"buggy", "fixed"}:
            coverage_failures.append(f"paired version evidence is missing: {key!r}")
            continue
        buggy = versions.get("buggy", {})
        fixed = versions.get("fixed", {})
        if (
            buggy.get("checkpoint_sha256") != fixed.get("checkpoint_sha256")
            or fixed.get("synthesis_cache_hit") is not True
            or fixed.get("checkpoint_reuse_success") is not True
            or fixed.get("actual_synthesis_calls") != 0
        ):
            failures.append(f"State checkpoint reuse drift: {key!r}")
    groups = index.get("groups") or []
    if (
        index.get("complete") is not True
        or len(groups) != group_count
        or any(group.get("complete") is not True for group in groups)
    ):
        coverage_failures.append(f"run index does not contain {group_count} complete groups")
    index_cells = 0
    for group in groups:
        cells = group.get("cells") or []
        index_cells += len(cells)
        for cell in cells:
            path = Path(str(cell.get("path") or ""))
            if not path.is_file() or _sha(path) != cell.get("sha256"):
                failures.append(f"cell snapshot SHA drift: {path}")
    if index_cells != count:
        coverage_failures.append(f"run index does not reference {count} cells")
    if index_cells != len(reports):
        failures.append("run index and observed report counts differ")
    return {
        "schema": "c1-lazarus-p2p-only-coverage-audit-v2",
        "complete": not failures and not coverage_failures,
        "evidence_integrity_complete": not failures,
        "coverage_complete": not coverage_failures and not (observed - expected or duplicates),
        "integrity_failures": failures,
        "coverage_failures": coverage_failures,
        "failures": [*failures, *coverage_failures],
        "missing_trial_keys": [list(key) for key in sorted(expected - observed)],
        "run_sha256": _sha(run_path),
        "index_sha256": _sha(index_path),
        "expected_cells": len(expected),
        "observed_cells": len(reports),
        "complete_groups": sum(group.get("complete") is True for group in groups),
        "start_successes": sum(row.get("start_success") is True for row in reports),
        "goal_successes": sum(row.get("goal_success") is True for row in reports),
    }


def audit_collection(root: Path) -> dict[str, Any]:
    from warptest.lazarus_collection import SCHEMA, key
    from warptest.lazarus_p2p_only_experiment import FORMAL_MODELS
    payload = json.loads((root / "collection.json").read_text())
    failures = []
    if payload.get("schema") != SCHEMA or payload.get("complete") is not True:
        failures.append("collection is incomplete or has invalid schema")
    expected = {key(row) for row in payload["plan"]["cells"]}
    registered = set(product(PAPER_TASK_IDS, FORMAL_MODELS, (0,),
                             ("lazarus_agent_s", "lazarus_agent_s_synth", "lazarus_state", "lazarus_scalecua", "lazarus_scalecua_synth"), ("buggy", "fixed")))
    if expected != registered:
        failures.append("collection plan differs from frozen four-model seed0 population")
    rows = payload["reports"]
    if len(rows) != len(expected) or {key(row) for row in rows} != expected:
        failures.append("collection coverage drift")
    if any(payload.get(field) is not False for field in ("formal", "confirmatory", "paper_ready")):
        failures.append("historical collection status drift")
    for path, expected_sha in payload["audit"]["evidence_files"].items():
        if not Path(path).is_file() or _sha(Path(path)) != expected_sha:
            failures.append(f"collection source evidence drift: {path}")
    return {"schema": "c1-lazarus-collection-delivery-audit-v1", "complete": not failures,
            "expected_cells": len(expected), "observed_cells": len(rows),
            "source_files_verified": len(payload["audit"]["evidence_files"]),
            "collection_sha256": _sha(root / "collection.json"),
            "failures": failures}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.run_root.expanduser().resolve()
    output = args.output.expanduser().resolve() if args.output else root / "coverage-audit.json"
    payload = audit(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
