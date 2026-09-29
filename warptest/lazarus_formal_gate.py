"""Construction-test qualification for formal C1-Lazarus candidates."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .lazarus_formal_build import (
    formal_construction_prepare_commands,
    formal_construction_runtime_environment,
)
from .lazarus_gate import evaluate_construction_test_matrix
from .lazarus_upstream import (
    definitions_from_upstream_record,
    frozen_sources_from_upstream_record,
    run_lazarus_construction_test_subset,
    validate_subprocess_output_capture,
)


def run_registered_construction_test_matrix(
    *,
    task_id: str,
    evidence_root: str | Path,
    fixed_root: str | Path,
    buggy_root: str | Path,
    sandbox_root: str | Path,
    harness_root: str | Path,
    total_timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Dispatch a construction matrix to its registered gate adapter.

    Most candidates use the ordinary frozen-upstream matrix below.  The one
    registered OpenRA GUI-differential F2P is deliberately excluded from that
    generic path: its task-private adapter additionally verifies the real
    replay, hidden oracle, and visual construction evidence.  Keeping this
    dispatch closed over the explicit task ID prevents a non-upstream F2P
    from silently receiving the exception.
    """

    if task_id == "lz-openra-21453-hotkey-ready-building":
        # Import lazily so the ordinary construction path has no OpenRA
        # runtime dependency and the provider-specific adapter remains the
        # sole owner of this exception.
        from .lazarus_openra_hotkey_gate import (
            run_openra_pr21453_revalidation_matrix,
        )

        return run_openra_pr21453_revalidation_matrix(
            task_id=task_id,
            evidence_root=evidence_root,
            fixed_root=fixed_root,
            buggy_root=buggy_root,
            sandbox_root=sandbox_root,
            harness_root=harness_root,
            total_timeout_s=total_timeout_s,
        )
    return run_formal_construction_test_matrix(
        task_id=task_id,
        evidence_root=evidence_root,
        fixed_root=fixed_root,
        buggy_root=buggy_root,
        sandbox_root=sandbox_root,
        harness_root=harness_root,
        total_timeout_s=total_timeout_s,
    )


def run_formal_construction_test_matrix(
    *,
    task_id: str,
    evidence_root: str | Path,
    fixed_root: str | Path,
    buggy_root: str | Path,
    sandbox_root: str | Path,
    harness_root: str | Path,
    total_timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Run frozen F2P/P2P once on fixed and buggy materialized products."""

    evidence = Path(evidence_root).expanduser().resolve()
    record = _load_mapping(evidence / "upstream.json")
    if record.get("task_id") != task_id:
        raise ValueError("formal construction evidence task identity drift")
    raw_tests = record.get("tests")
    if not isinstance(raw_tests, list):
        raise ValueError("formal construction evidence has no tests")
    selected: dict[str, list[str]] = {"fail_to_pass": [], "pass_to_pass": []}
    for raw in raw_tests:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind") or "")
        if kind not in selected:
            continue
        if kind == "fail_to_pass" and raw.get("origin") != "upstream":
            raise ValueError(
                "issue-derived OpenRA F2P requires its registered GUI gate adapter"
            )
        selected[kind].append(str(raw.get("id") or ""))
    if (
        len(selected["fail_to_pass"]) != 1
        or not selected["fail_to_pass"][0]
        or not selected["pass_to_pass"]
        or any(not value for value in selected["pass_to_pass"])
    ):
        raise ValueError(
            "formal construction matrix requires one F2P and at least one P2P"
        )

    definitions = definitions_from_upstream_record(record)
    executions: list[dict[str, Any]] = []
    outcomes: dict[str, dict[str, bool]] = {"fixed": {}, "buggy": {}}
    # The source descriptor may register several independent P2P controls.
    # Run every registered one rather than discarding all but the first: a
    # candidate only qualifies when its full frozen construction table is
    # stable on both fixed and buggy materializations.
    test_ids = [selected["fail_to_pass"][0], *selected["pass_to_pass"]]
    prepare_commands = formal_construction_prepare_commands(
        task_id,
        test_cwds=tuple(definitions[test_id].cwd for test_id in test_ids),
    )
    runtime_environment_factory = formal_construction_runtime_environment(
        task_id,
        test_cwds=tuple(definitions[test_id].cwd for test_id in test_ids),
    )
    terminal_failure_classes: set[str] = set()
    incomplete_nonterminal_failures: list[str] = []
    for version, source in (("fixed", fixed_root), ("buggy", buggy_root)):
        result = run_lazarus_construction_test_subset(
            source,
            definitions,
            test_ids,
            sandbox_root=sandbox_root,
            frozen_source_root=evidence,
            frozen_sources=frozen_sources_from_upstream_record(record, test_ids),
            prepare_commands=prepare_commands,
            runtime_environment_factory=runtime_environment_factory,
            harness_root=harness_root,
            total_timeout_s=total_timeout_s,
        )
        if result.terminal_failure_class is not None:
            terminal_failure_classes.add(result.terminal_failure_class)
            # A registered construction incompatibility happens before an
            # upstream F2P/P2P command can run.  Preserve it as a structured
            # terminal instead of throwing and degrading it into a generic
            # runner outage.  The false table is deliberately non-qualifying.
            for test_id in test_ids:
                outcomes[version][test_id] = False
        elif not _has_bounded_upstream_results(result, test_ids):
            # Do not invent an F2P/P2P outcome when a coordinator-owned
            # preparation/runtime step stopped before a bounded upstream test
            # matrix existed.  ``result`` itself is still emitted below so
            # both version-specific preparation receipts and failures survive
            # through revalidation.
            if result.failures:
                incomplete_nonterminal_failures.extend(
                    f"{version}: {failure}" for failure in result.failures
                )
            else:
                incomplete_nonterminal_failures.append(
                    f"{version}: construction execution was incomplete"
                )
        else:
            for test_id in test_ids:
                outcomes[version][test_id] = _observed_pass(result, test_id)
        executions.append(_construction_execution_receipt(version, result))
    if len(terminal_failure_classes) > 1:
        raise ValueError("formal construction has conflicting terminal failures")
    terminal_failure_class = next(iter(terminal_failure_classes), None)
    if incomplete_nonterminal_failures:
        failures = list(incomplete_nonterminal_failures)
        if terminal_failure_class is not None:
            failures.append(
                f"registered construction terminal: {terminal_failure_class}"
            )
        return {
            "schema": "c1-lazarus-formal-construction-tests-v1",
            "schema_version": 1,
            "task_id": task_id,
            "record_sha256": record.get("record_sha256"),
            "patch_sha256": record.get("patch_sha256"),
            "complete": False,
            "qualified": False,
            # An empty table is intentional here.  The commands never
            # reached a measured F2P/P2P result, so ``False`` would be a
            # fabricated verdict rather than a test observation.
            "fixed_tests": {},
            "buggy_tests": {},
            "failures": failures,
            "executions": executions,
            "terminal_failure_class": terminal_failure_class,
        }
    matrix = evaluate_construction_test_matrix(
        task_id=task_id,
        fail_to_pass=tuple(selected["fail_to_pass"]),
        pass_to_pass=tuple(selected["pass_to_pass"]),
        fixed_tests=outcomes["fixed"],
        buggy_tests=outcomes["buggy"],
    )
    failures = list(matrix.failures)
    if terminal_failure_class is not None:
        failures.append(f"registered construction terminal: {terminal_failure_class}")
    return {
        "schema": "c1-lazarus-formal-construction-tests-v1",
        "schema_version": 1,
        "task_id": task_id,
        "record_sha256": record.get("record_sha256"),
        "patch_sha256": record.get("patch_sha256"),
        "complete": matrix.complete and terminal_failure_class is None,
        "qualified": matrix.qualified and terminal_failure_class is None,
        "fixed_tests": matrix.fixed_tests,
        "buggy_tests": matrix.buggy_tests,
        "failures": failures,
        "executions": executions,
        "terminal_failure_class": terminal_failure_class,
    }


def write_formal_construction_test_report(
    report: dict[str, Any], path: str | Path
) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if destination.exists():
        if destination.read_text(encoding="utf-8") != encoded:
            raise ValueError(
                f"refusing to overwrite formal construction evidence: {destination}"
            )
        return destination
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(encoded, encoding="utf-8")
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _observed_pass(result: Any, test_id: str) -> bool:
    rows = tuple(row for row in result.results if row.id == test_id)
    if len(rows) != 1:
        raise ValueError(
            f"formal construction test {test_id} was not executed exactly once"
        )
    row = rows[0]
    if row.timed_out or row.exit_code is None:
        raise ValueError(f"formal construction test {test_id} has no bounded outcome")
    return row.passed is True


def _has_bounded_upstream_results(result: Any, test_ids: list[str]) -> bool:
    """Whether an execution reached one bounded observation per upstream test.

    A non-zero upstream F2P is a valid (and often expected) observation, so
    ``ConstructionSubsetResult.complete`` cannot distinguish it from a setup
    failure: that field is intentionally false for either a failed test or an
    incomplete command sequence.  Only the per-test receipt tells the formal
    matrix whether it may project an F2P/P2P truth table.
    """

    rows = tuple(result.results)
    return (
        len(rows) == len(test_ids)
        and {row.id for row in rows} == set(test_ids)
        and len({row.id for row in rows}) == len(rows)
        and all(not row.timed_out and row.exit_code is not None for row in rows)
    )


def _construction_execution_receipt(version: str, result: Any) -> dict[str, Any]:
    """Serialize a construction clone only after its raw captures validate."""

    if version not in {"fixed", "buggy"}:
        raise ValueError("formal construction execution version is invalid")
    receipt = asdict(result)
    for field, label in (
        ("results", "upstream"),
        ("prepare_results", "preparation"),
    ):
        rows = receipt.get(field)
        if type(rows) not in {list, tuple}:
            raise ValueError("formal construction execution receipt is invalid")
        for row in rows:
            if type(row) is not dict:
                raise ValueError("formal construction execution receipt is invalid")
            try:
                validate_subprocess_output_capture(
                    row.get("output_capture"),
                    label=f"formal construction {label}",
                )
            except ValueError as error:
                raise ValueError(
                    "formal construction output capture is invalid"
                ) from error
    return {"version": version, **receipt}


def _load_mapping(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"formal construction evidence is not a mapping: {path}")
    return value
