"""Construction gate for the OpenRA PR #21453 Phase-0 candidate."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from .c1_sandbox import strict_clone_tree
from .c1_evaluation import validate_evaluation_contract
from .lazarus_gate import (
    ConstructionGateEvidence,
    ConstructionGateResult,
    evaluate_construction_gate,
)
from .lazarus_goldens import LazarusGoldenVerification, validate_lazarus_golden_set
from .lazarus_mining import validate_lazarus_trial_provenance
from .lazarus_openra_hotkey_probe import (
    OPENRA_HOTKEY_PHASE0_TASK_ID,
    OPENRA_HOTKEY_REGISTERED_START_SHA256,
    create_openra_pr21453_prebuild_callback,
    run_openra_pr21453_hotkey_cycle_probe,
)
from .lazarus_upstream import (
    UpstreamSubsetResult,
    definitions_from_upstream_record,
    run_upstream_test_subset,
)
from .lazarus_versions import (
    LazarusVersionArtifact,
    LazarusVersionPair,
    LazarusVersionRequest,
    materialize_lazarus_versions,
)


OPENRA_HOTKEY_GATE_SANDBOX_ROOT = Path("/private/tmp/warptest-lazarus-gates")
_F2P_IDS = ("openra-pr21453-hidden-ready-building-f2p",)
_P2P_IDS = ("openra-action-queue-p2p",)
_AUDIT_TYPE = "openra_lazarus_hotkey_audit"
OPENRA_HOTKEY_REVALIDATION_ADAPTER_ID = "openra-pr21453-gui-gate-v1"


@dataclass(frozen=True)
class OpenRAHotkeySemanticObservation:
    """One coordinator-only projection of a real PR #21453 probe report."""

    status: str
    start_ok: bool
    primary_ok: bool
    invariants_ok: bool
    report_sha256: str | None
    checks: tuple[str, ...]


@dataclass(frozen=True)
class OpenRAHotkeyConstructionGateRun:
    result: ConstructionGateResult
    golden_verification: LazarusGoldenVerification
    fixed_upstream: UpstreamSubsetResult
    buggy_upstream: UpstreamSubsetResult
    fixed_start: OpenRAHotkeySemanticObservation
    buggy_start: OpenRAHotkeySemanticObservation
    fixed_goal: OpenRAHotkeySemanticObservation
    buggy_signature: OpenRAHotkeySemanticObservation
    fixed_signature: OpenRAHotkeySemanticObservation
    report_path: Path


def evaluate_openra_pr21453_gate(
    *,
    fixed_tests: Mapping[str, bool],
    buggy_tests: Mapping[str, bool],
    goldens: LazarusGoldenVerification,
    fixed_start: OpenRAHotkeySemanticObservation,
    buggy_start: OpenRAHotkeySemanticObservation,
    fixed_goal: OpenRAHotkeySemanticObservation,
    buggy_signature: OpenRAHotkeySemanticObservation,
    fixed_signature: OpenRAHotkeySemanticObservation,
    provenance_verified: bool,
) -> ConstructionGateResult:
    """Apply OpenRA's registered GUI-differential F2P exception exactly once."""

    return evaluate_construction_gate(
        ConstructionGateEvidence(
            task_id=OPENRA_HOTKEY_PHASE0_TASK_ID,
            benchmark="openra",
            f2p_origin="issue_derived_gui_differential",
            fail_to_pass=_F2P_IDS,
            pass_to_pass=_P2P_IDS,
            fixed_tests=dict(fixed_tests),
            buggy_tests=dict(buggy_tests),
            provenance_verified=provenance_verified and goldens.complete,
            fixed_before_semantic=fixed_start.start_ok,
            buggy_before_semantic=buggy_start.start_ok,
            fixed_before_visual=goldens.fixed_before_visual,
            buggy_before_visual=goldens.buggy_before_visual,
            before_equivalent=goldens.before_equivalent,
            fixed_goal_checks=fixed_goal.primary_ok,
            fixed_after_visual=goldens.fixed_after_visual,
            buggy_defect_signature=buggy_signature.primary_ok,
            buggy_after_visual=goldens.buggy_after_visual,
            fixed_defect_signature=fixed_signature.primary_ok,
            fixed_invariants=fixed_goal.invariants_ok and fixed_signature.invariants_ok,
            buggy_invariants=buggy_signature.invariants_ok,
        )
    )


def run_openra_pr21453_construction_gate(
    pair: LazarusVersionPair,
    *,
    repository_root: str | Path,
    spec_path: str | Path,
    upstream_record_path: str | Path,
    golden_metadata_path: str | Path,
    report_path: str | Path,
    sandbox_root: str | Path = OPENRA_HOTKEY_GATE_SANDBOX_ROOT,
) -> OpenRAHotkeyConstructionGateRun:
    """Run all non-paid OpenRA PR #21453 construction checks in strict clones."""

    _validate_pair(pair)
    root = Path(repository_root).expanduser().resolve()
    hidden_spec_path = Path(spec_path).expanduser().resolve()
    upstream_path = Path(upstream_record_path).expanduser().resolve()
    if (
        not root.is_dir()
        or not hidden_spec_path.is_file()
        or not upstream_path.is_file()
    ):
        raise ValueError("OpenRA construction gate inputs must exist")
    spec = _load_yaml(hidden_spec_path)
    spec_errors = validate_evaluation_contract(
        spec, hidden_spec_path, allow_lazarus=True
    )
    if spec_errors:
        raise ValueError("OpenRA Lazarus oracle is invalid: " + "; ".join(spec_errors))
    _validate_registered_oracle(spec)
    record = _load_json(upstream_path)
    provenance_errors = validate_lazarus_trial_provenance(
        record, source_root=pair.fixed.root
    )
    definitions = definitions_from_upstream_record(record)
    _validate_registered_test_ids(spec, definitions)
    golden = validate_lazarus_golden_set(
        golden_metadata_path,
        task_id=OPENRA_HOTKEY_PHASE0_TASK_ID,
    )
    provenance_verified = not provenance_errors and _provenance_matches_pair(
        pair, record
    )

    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    fixed_upstream = run_upstream_test_subset(
        pair.fixed.root,
        definitions,
        (*_F2P_IDS, *_P2P_IDS),
        sandbox_root=sandbox,
        harness_root=root,
        trusted_git_dir=pair.fixed.git_metadata,
    )
    buggy_upstream = run_upstream_test_subset(
        pair.buggy.root,
        definitions,
        (*_F2P_IDS, *_P2P_IDS),
        sandbox_root=sandbox,
        harness_root=root,
        trusted_git_dir=pair.buggy.git_metadata,
    )

    with tempfile.TemporaryDirectory(
        prefix="openra-pr21453-semantic-", dir=sandbox
    ) as temp_dir:
        workspace = Path(temp_dir).resolve()
        fixed_views = _run_semantic_views(pair.fixed, workspace)
        buggy_views = _run_semantic_views(pair.buggy, workspace)

    result = evaluate_openra_pr21453_gate(
        fixed_tests=_outcomes(fixed_upstream),
        buggy_tests=_outcomes(buggy_upstream),
        goldens=golden,
        fixed_start=fixed_views["start"],
        buggy_start=buggy_views["start"],
        fixed_goal=fixed_views["goal"],
        buggy_signature=buggy_views["defect"],
        fixed_signature=fixed_views["defect"],
        provenance_verified=provenance_verified,
    )
    destination = _write_report(
        report_path,
        result=result,
        golden=golden,
        fixed_upstream=fixed_upstream,
        buggy_upstream=buggy_upstream,
        semantic={
            "fixed_start": fixed_views["start"],
            "buggy_start": buggy_views["start"],
            "fixed_goal": fixed_views["goal"],
            "buggy_signature": buggy_views["defect"],
            "fixed_signature": fixed_views["defect"],
        },
        provenance_errors=provenance_errors,
        pair=pair,
    )
    return OpenRAHotkeyConstructionGateRun(
        result=result,
        golden_verification=golden,
        fixed_upstream=fixed_upstream,
        buggy_upstream=buggy_upstream,
        fixed_start=fixed_views["start"],
        buggy_start=buggy_views["start"],
        fixed_goal=fixed_views["goal"],
        buggy_signature=buggy_views["defect"],
        fixed_signature=fixed_views["defect"],
        report_path=destination,
    )


def run_openra_pr21453_revalidation_matrix(
    *,
    task_id: str,
    evidence_root: str | Path,
    fixed_root: str | Path,
    buggy_root: str | Path,
    sandbox_root: str | Path,
    harness_root: str | Path,
    total_timeout_s: float = 600.0,
) -> dict[str, Any]:
    """Project the registered GUI gate into curated-revalidation matrix form.

    This is intentionally not a generic workaround for issue-derived F2P
    tests.  The task-specific gate executes its existing OpenRA replay,
    golden, and hidden-oracle checks, then this adapter retains both the
    upstream truth table and the full gate receipt for the stable selection
    artifact.
    """

    if task_id != OPENRA_HOTKEY_PHASE0_TASK_ID:
        raise ValueError("OpenRA revalidation adapter task identity drift")
    if (
        type(total_timeout_s) not in {int, float}
        or isinstance(total_timeout_s, bool)
        or not 0 < float(total_timeout_s) <= 1_200
    ):
        raise ValueError("OpenRA revalidation timeout is invalid")
    root = Path(harness_root).expanduser().resolve()
    evidence = Path(evidence_root).expanduser().resolve()
    if not root.is_dir() or not evidence.is_dir():
        raise ValueError("OpenRA revalidation roots must exist")
    fixed = Path(fixed_root).expanduser().resolve()
    buggy = Path(buggy_root).expanduser().resolve()
    pair = _load_openra_pr21453_revalidation_pair(
        fixed_root=fixed,
        buggy_root=buggy,
        harness_root=root,
    )
    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    report_path = (
        sandbox
        / "registered-gate-reports"
        / OPENRA_HOTKEY_PHASE0_TASK_ID
        / f"{os.getpid()}-{time.time_ns()}.json"
    )
    gate = run_openra_pr21453_construction_gate(
        pair,
        repository_root=root,
        spec_path=(
            root / "specs/openra/lazarus/lz-openra-21453-hotkey-ready-building.yaml"
        ),
        upstream_record_path=evidence / "upstream.json",
        golden_metadata_path=root / "specs/openra/lazarus/visual/goldens.meta.json",
        report_path=report_path,
        sandbox_root=sandbox,
    )
    return _openra_pr21453_revalidation_report(gate)


def _load_openra_pr21453_revalidation_pair(
    *,
    fixed_root: Path,
    buggy_root: Path,
    harness_root: Path,
) -> LazarusVersionPair:
    """Load, but never create, the registered prebuilt version pair."""

    task_root = fixed_root.parent
    if (
        fixed_root.name != "fixed"
        or buggy_root.name != "buggy"
        or task_root.name != OPENRA_HOTKEY_PHASE0_TASK_ID
        or buggy_root.parent != task_root
        or task_root.parent.name != "prebuilt"
        or not fixed_root.is_dir()
        or not buggy_root.is_dir()
    ):
        raise ValueError(
            "OpenRA revalidation pair does not use its registered prebuilt roots"
        )
    cache_root = task_root.parent.parent
    metadata_root = cache_root / "metadata" / OPENRA_HOTKEY_PHASE0_TASK_ID
    if not metadata_root.is_dir() or not (metadata_root / "manifest.json").is_file():
        raise ValueError("OpenRA revalidation requires its existing prebuilt pair")
    request = _openra_pr21453_version_request(harness_root)
    # ``materialize_lazarus_versions`` validates the existing manifest and
    # both materialized trees.  The prebuilt root check above makes a cache
    # miss fail closed, so this adapter cannot silently start a fresh build.
    pair = materialize_lazarus_versions(
        request,
        cache_root=cache_root,
        build=create_openra_pr21453_prebuild_callback(),
    )
    if pair.fixed.root != fixed_root or pair.buggy.root != buggy_root:
        raise ValueError("OpenRA revalidation cache pair root drift")
    return pair


def _openra_pr21453_version_request(harness_root: Path) -> LazarusVersionRequest:
    path = (
        harness_root / "benchmarks/p5/lazarus/authoring/"
        "lz-openra-21453-hotkey-ready-building.version-request.json"
    )
    payload = _load_json(path)
    expected = {
        "task_id",
        "source_repo",
        "fixed_revision",
        "revert_patch",
        "patch_sha256",
        "declared_files",
        "build_class",
    }
    if set(payload) != expected:
        raise ValueError("OpenRA revalidation version request schema drift")
    source_repo = _registered_relative_path(
        harness_root, payload.get("source_repo"), "source repository"
    )
    revert_patch = _registered_relative_path(
        harness_root, payload.get("revert_patch"), "revert patch"
    )
    declared = payload.get("declared_files")
    if not isinstance(declared, list) or not all(
        isinstance(value, str) and value for value in declared
    ):
        raise ValueError("OpenRA revalidation declared files are invalid")
    return LazarusVersionRequest(
        task_id=str(payload.get("task_id") or ""),
        source_repo=source_repo,
        fixed_revision=str(payload.get("fixed_revision") or ""),
        revert_patch=revert_patch,
        patch_sha256=str(payload.get("patch_sha256") or ""),
        declared_files=tuple(declared),
        build_class=str(payload.get("build_class") or ""),
    )


def _registered_relative_path(root: Path, value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError(f"OpenRA revalidation {label} is invalid")
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"OpenRA revalidation {label} escapes the harness")
    resolved = (root / relative).resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"OpenRA revalidation {label} escapes the harness")
    return resolved


def _openra_pr21453_revalidation_report(
    gate: OpenRAHotkeyConstructionGateRun,
) -> dict[str, Any]:
    result = gate.result
    if (
        result.task_id != OPENRA_HOTKEY_PHASE0_TASK_ID
        or result.benchmark != "openra"
        or result.f2p_origin != "issue_derived_gui_differential"
        or result.f2p_claim != "issue_derived_gui_differential_not_upstream"
        or tuple(result.fail_to_pass) != _F2P_IDS
        or tuple(result.pass_to_pass) != _P2P_IDS
    ):
        raise ValueError("OpenRA revalidation registered gate receipt drift")
    fixed_tests = _outcomes(gate.fixed_upstream)
    buggy_tests = _outcomes(gate.buggy_upstream)
    expected_test_ids = set(_F2P_IDS + _P2P_IDS)
    if set(fixed_tests) != expected_test_ids or set(buggy_tests) != expected_test_ids:
        raise ValueError("OpenRA revalidation registered test table is incomplete")
    receipt = result.to_report()
    checks = receipt.get("checks")
    if not isinstance(checks, dict) or any(
        not isinstance(name, str) or not isinstance(value, bool)
        for name, value in checks.items()
    ):
        raise ValueError("OpenRA revalidation registered gate checks are invalid")
    if (
        checks.get("fixed_f2p") is not all(fixed_tests[test_id] for test_id in _F2P_IDS)
        or checks.get("buggy_f2p")
        is not any(buggy_tests[test_id] for test_id in _F2P_IDS)
        or checks.get("fixed_p2p")
        is not all(fixed_tests[test_id] for test_id in _P2P_IDS)
        or checks.get("buggy_p2p")
        is not all(buggy_tests[test_id] for test_id in _P2P_IDS)
    ):
        raise ValueError("OpenRA revalidation registered gate truth table drift")
    return {
        "schema": "c1-lazarus-openra-pr21453-revalidation-adapter-v1",
        "schema_version": 1,
        "task_id": OPENRA_HOTKEY_PHASE0_TASK_ID,
        "complete": result.complete,
        "qualified": result.passed,
        "fixed_tests": fixed_tests,
        "buggy_tests": buggy_tests,
        "failures": list(result.failures),
        "executions": [
            _openra_revalidation_execution("fixed", gate.fixed_upstream),
            _openra_revalidation_execution("buggy", gate.buggy_upstream),
        ],
        "registered_gate": {
            "adapter_id": OPENRA_HOTKEY_REVALIDATION_ADAPTER_ID,
            "task_id": result.task_id,
            "complete": result.complete,
            "passed": result.passed,
            "checks": dict(checks),
            "failures": list(result.failures),
            "f2p_claim": result.f2p_claim,
            "f2p_origin": result.f2p_origin,
            "fail_to_pass": list(result.fail_to_pass),
            "pass_to_pass": list(result.pass_to_pass),
            "report_path": str(gate.report_path),
        },
    }


def _openra_revalidation_execution(
    version: str, subset: UpstreamSubsetResult
) -> dict[str, Any]:
    if version not in {"fixed", "buggy"}:
        raise ValueError("OpenRA revalidation version is invalid")
    return {
        "version": version,
        "duration_ms": subset.duration_ms,
        "results": [asdict(row) for row in subset.results],
        "prepare_results": [],
        "terminal_failure_class": None,
    }


def _run_semantic_views(
    artifact: LazarusVersionArtifact,
    workspace: Path,
) -> dict[str, OpenRAHotkeySemanticObservation]:
    trial = workspace / f"{artifact.version}-trial"
    strict_clone_tree(artifact.root, trial)
    output = workspace / f"{artifact.version}-output"
    output.mkdir(parents=True, exist_ok=False)
    summary = run_openra_pr21453_hotkey_cycle_probe(
        trial,
        output,
        checkpoint_sha256=OPENRA_HOTKEY_REGISTERED_START_SHA256,
    )
    report = output / "openra-pr21453-hotkey-semantic-report.json"
    _atomic_json(report, summary)
    report_sha = _sha256(report)
    before = summary.get("before") if isinstance(summary.get("before"), Mapping) else {}
    after = summary.get("after") if isinstance(summary.get("after"), Mapping) else {}
    start_ok = (
        before.get("executed") is True
        and before.get("ready") is True
        and before.get("state_restore_applied") is True
    )
    invariants_ok = (
        after.get("executed") is True
        and after.get("invariants_ok") is True
        and after.get("placement_cancel_observed") is True
    )
    status = str(summary.get("status") or "failure")
    checks = (
        f"before.ready:{str(start_ok).lower()}",
        f"after.goal_checks:{str(after.get('goal_checks_ok') is True).lower()}",
        f"after.defect_signature:{str(after.get('defect_signature_ok') is True).lower()}",
        f"after.invariants:{str(invariants_ok).lower()}",
    )
    return {
        "start": OpenRAHotkeySemanticObservation(
            status=status,
            start_ok=start_ok,
            primary_ok=start_ok,
            invariants_ok=invariants_ok,
            report_sha256=report_sha,
            checks=checks,
        ),
        "goal": OpenRAHotkeySemanticObservation(
            status=status,
            start_ok=start_ok,
            primary_ok=after.get("goal_checks_ok") is True,
            invariants_ok=invariants_ok,
            report_sha256=report_sha,
            checks=checks,
        ),
        "defect": OpenRAHotkeySemanticObservation(
            status=status,
            start_ok=start_ok,
            primary_ok=after.get("defect_signature_ok") is True,
            invariants_ok=invariants_ok,
            report_sha256=report_sha,
            checks=checks,
        ),
    }


def _validate_pair(pair: LazarusVersionPair) -> None:
    if (
        pair.fixed.task_id != OPENRA_HOTKEY_PHASE0_TASK_ID
        or pair.buggy.task_id != OPENRA_HOTKEY_PHASE0_TASK_ID
        or pair.fixed.version != "fixed"
        or pair.buggy.version != "buggy"
        or pair.fixed.patch_sha256 != pair.buggy.patch_sha256
    ):
        raise ValueError("OpenRA construction gate requires its matched Phase-0 pair")


def _provenance_matches_pair(
    pair: LazarusVersionPair, record: Mapping[str, Any]
) -> bool:
    return (
        record.get("task_id") == OPENRA_HOTKEY_PHASE0_TASK_ID
        and record.get("benchmark") == "openra"
        and record.get("pinned_revision") == pair.fixed.source_revision
        and record.get("patch_sha256") == pair.fixed.patch_sha256
        and record.get("patch_sha256") == pair.buggy.patch_sha256
    )


def _validate_registered_test_ids(
    spec: Mapping[str, Any], definitions: Mapping[str, Any]
) -> None:
    lazarus = (spec.get("evaluation") or {}).get("lazarus")
    if not isinstance(lazarus, Mapping):
        raise ValueError("OpenRA construction gate requires evaluation.lazarus")
    upstream = lazarus.get("upstream_tests")
    if not isinstance(upstream, Mapping):
        raise ValueError("OpenRA construction gate requires upstream test ids")
    if tuple(upstream.get("fail_to_pass") or ()) != _F2P_IDS:
        raise ValueError("OpenRA construction gate F2P registration drift")
    if tuple(upstream.get("pass_to_pass") or ()) != _P2P_IDS:
        raise ValueError("OpenRA construction gate P2P registration drift")
    if tuple(upstream.get("per_trial_p2p") or ()) != _P2P_IDS:
        raise ValueError("OpenRA construction gate per-trial P2P registration drift")
    if not set(_F2P_IDS + _P2P_IDS) <= set(definitions):
        raise ValueError("OpenRA construction gate upstream record is incomplete")
    if definitions[_F2P_IDS[0]].origin != "issue_derived_gui_differential":
        raise ValueError("OpenRA construction gate F2P origin drift")
    if definitions[_P2P_IDS[0]].origin != "upstream":
        raise ValueError("OpenRA construction gate P2P origin drift")


def _validate_registered_oracle(spec: Mapping[str, Any]) -> None:
    lazarus = (spec.get("evaluation") or {}).get("lazarus")
    if not isinstance(lazarus, Mapping):
        raise ValueError("OpenRA construction gate requires evaluation.lazarus")
    expected = {
        "goal_checks": [
            {"type": _AUDIT_TYPE, "field": "placement_active", "expected": True}
        ],
        "defect_signature": [
            {"type": _AUDIT_TYPE, "field": "placement_active", "expected": False}
        ],
        "invariant_checks": [
            {"type": _AUDIT_TYPE, "field": "ready_hidden_building", "expected": True},
            {
                "type": _AUDIT_TYPE,
                "field": "placement_cancel_observed",
                "expected": True,
            },
            {"type": "no_openra_gameplay_probe_errors"},
        ],
    }
    if {key: lazarus.get(key) for key in expected} != expected:
        raise ValueError("OpenRA construction gate oracle registration drift")


def _outcomes(result: UpstreamSubsetResult) -> dict[str, bool]:
    return {row.id: row.passed for row in result.results}


def _write_report(
    path: str | Path,
    *,
    result: ConstructionGateResult,
    golden: LazarusGoldenVerification,
    fixed_upstream: UpstreamSubsetResult,
    buggy_upstream: UpstreamSubsetResult,
    semantic: Mapping[str, OpenRAHotkeySemanticObservation],
    provenance_errors: list[str],
    pair: LazarusVersionPair,
) -> Path:
    destination = Path(path).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = result.to_report()
    payload["evidence"] = {
        "goldens": asdict(golden),
        "upstream": {
            "fixed": _subset_payload(fixed_upstream),
            "buggy": _subset_payload(buggy_upstream),
        },
        "semantic": {name: asdict(value) for name, value in semantic.items()},
        "provenance_errors": list(provenance_errors),
        "versions": {
            "fixed": _artifact_payload(pair.fixed),
            "buggy": _artifact_payload(pair.buggy),
        },
    }
    _atomic_json(destination, payload)
    return destination


def _subset_payload(result: UpstreamSubsetResult) -> dict[str, Any]:
    return {
        "test_ids": list(result.test_ids),
        "complete": result.complete,
        "passed": result.passed,
        "failures": list(result.failures),
        "isolation": result.isolation,
        "duration_ms": result.duration_ms,
        "results": [asdict(value) for value in result.results],
    }


def _artifact_payload(artifact: LazarusVersionArtifact) -> dict[str, Any]:
    return {
        "source_revision": artifact.source_revision,
        "patch_sha256": artifact.patch_sha256,
        "tree_sha256": artifact.tree_sha256,
        "changed_files": list(artifact.changed_files),
        "build_identity": artifact.build_identity,
    }


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("OpenRA construction gate spec must be a mapping")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("OpenRA construction gate upstream record must be a mapping")
    return value


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
