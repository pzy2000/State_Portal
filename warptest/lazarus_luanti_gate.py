"""Construction-gate assembly and execution for the Luanti Phase-0 task."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

import yaml

from .c1_sandbox import strict_clone_tree
from .c1_evaluation import validate_evaluation_contract
from .lazarus_catalog import materialize_lazarus_spec
from .lazarus_gate import (
    ConstructionGateEvidence,
    ConstructionGateResult,
    evaluate_construction_gate,
)
from .lazarus_goldens import LazarusGoldenVerification, validate_lazarus_golden_set
from .lazarus_luanti_goldens import LUANTI_PHASE0_TASK_ID
from .lazarus_mining import validate_lazarus_trial_provenance
from .lazarus_probes import luanti_pr11919_public_view_assertion
from .lazarus_upstream import (
    UpstreamSubsetResult,
    definitions_from_upstream_record,
    run_upstream_test_subset,
)
from .lazarus_versions import LazarusVersionArtifact, LazarusVersionPair
from .luanti_runtime import LuantiRuntimeAdapter


LUANTI_GATE_SANDBOX_ROOT = Path("/private/tmp/warptest-lazarus-gates")
_F2P_IDS = ("luanti-pr11919-vector-from-string-f2p",)
_P2P_IDS = (
    "luanti-vector-zero-p2p",
    "luanti-vector-to-string-p2p",
)


@dataclass(frozen=True)
class LuantiGateProfile:
    """The registered identity of one Luanti Phase-0 construction gate.

    Every field is evidence the gate refuses to infer: which task it may run, the
    exact upstream ids its truth table applies to, and the public Start view it
    checks before either version is graded.
    """

    task_id: str
    fail_to_pass: tuple[str, ...]
    pass_to_pass: tuple[str, ...]
    per_trial_p2p: tuple[str, ...]
    start_view_assertion: Mapping[str, Any]
    label: str

    def __post_init__(self) -> None:
        if not self.task_id.startswith("lz-luanti-"):
            raise ValueError("Luanti gate profile task id is invalid")
        if not self.fail_to_pass or not self.pass_to_pass:
            raise ValueError("Luanti gate profile must register F2P and P2P ids")
        if not set(self.per_trial_p2p) <= set(self.pass_to_pass):
            raise ValueError("Luanti gate per-trial P2P must be a P2P subset")
        if set(self.fail_to_pass) & set(self.pass_to_pass):
            raise ValueError("Luanti gate F2P and P2P ids must be disjoint")
        if str(self.start_view_assertion.get("type") or "") != (
            "luanti_player_view_matches"
        ):
            raise ValueError("Luanti gate Start view must be a player-view assertion")


PR11919_GATE_PROFILE = LuantiGateProfile(
    task_id=LUANTI_PHASE0_TASK_ID,
    fail_to_pass=_F2P_IDS,
    pass_to_pass=_P2P_IDS,
    per_trial_p2p=(_P2P_IDS[0],),
    start_view_assertion=luanti_pr11919_public_view_assertion(),
    label="luanti-pr11919",
)
PR14757_GATE_PROFILE = LuantiGateProfile(
    task_id="lz-luanti-14757-math-round",
    fail_to_pass=("luanti-pr14757-math-round-f2p",),
    pass_to_pass=("luanti-pr14757-formspec-escape-p2p",),
    per_trial_p2p=("luanti-pr14757-formspec-escape-p2p",),
    # The rounding Start uses the same public camera as the coordinate Start.
    start_view_assertion=luanti_pr11919_public_view_assertion(),
    label="luanti-pr14757",
)
LUANTI_GATE_PROFILES: dict[str, LuantiGateProfile] = {
    profile.task_id: profile
    for profile in (PR11919_GATE_PROFILE, PR14757_GATE_PROFILE)
}


@dataclass(frozen=True)
class LuantiSemanticObservation:
    """Coordinator-side outcome of one exact hidden oracle projection."""

    status: str
    start_ok: bool
    view_ok: bool
    primary_ok: bool
    invariants_ok: bool
    report_sha256: str | None
    checks: tuple[str, ...]


@dataclass(frozen=True)
class LuantiConstructionGateRun:
    result: ConstructionGateResult
    golden_verification: LazarusGoldenVerification
    fixed_upstream: UpstreamSubsetResult
    buggy_upstream: UpstreamSubsetResult
    fixed_start: LuantiSemanticObservation
    buggy_start: LuantiSemanticObservation
    fixed_goal: LuantiSemanticObservation
    buggy_signature: LuantiSemanticObservation
    fixed_signature: LuantiSemanticObservation
    report_path: Path


def evaluate_luanti_gate(
    *,
    profile: LuantiGateProfile,
    fixed_tests: Mapping[str, bool],
    buggy_tests: Mapping[str, bool],
    goldens: LazarusGoldenVerification,
    fixed_start: LuantiSemanticObservation,
    buggy_start: LuantiSemanticObservation,
    fixed_goal: LuantiSemanticObservation,
    buggy_signature: LuantiSemanticObservation,
    fixed_signature: LuantiSemanticObservation,
    provenance_verified: bool,
) -> ConstructionGateResult:
    """Apply the locked Phase-0 truth table without trusting a trial verdict."""

    return evaluate_construction_gate(
        ConstructionGateEvidence(
            task_id=profile.task_id,
            benchmark="luanti",
            f2p_origin="upstream",
            fail_to_pass=profile.fail_to_pass,
            pass_to_pass=profile.pass_to_pass,
            fixed_tests=dict(fixed_tests),
            buggy_tests=dict(buggy_tests),
            provenance_verified=provenance_verified and goldens.complete,
            fixed_before_semantic=(
                fixed_start.status == "success"
                and fixed_start.start_ok
                and fixed_start.view_ok
            ),
            buggy_before_semantic=(
                buggy_start.status == "success"
                and buggy_start.start_ok
                and buggy_start.view_ok
            ),
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


def evaluate_luanti_pr11919_gate(**evidence: Any) -> ConstructionGateResult:
    """Evaluate the PR #11919 gate through the registered profile."""

    return evaluate_luanti_gate(profile=PR11919_GATE_PROFILE, **evidence)


def run_luanti_construction_gate(
    pair: LazarusVersionPair,
    *,
    profile: LuantiGateProfile,
    repository_root: str | Path,
    spec_path: str | Path,
    upstream_record_path: str | Path,
    golden_metadata_path: str | Path,
    report_path: str | Path,
    sandbox_root: str | Path = LUANTI_GATE_SANDBOX_ROOT,
) -> LuantiConstructionGateRun:
    """Run all real Phase-0 construction checks in isolated CoW clones.

    No model is contacted here.  The frozen upstream record supplies the exact
    F2P/P2P commands; semantic oracle views are run separately on both materialized
    trees; and visual evidence is verified from its atomically published triplet.
    """

    _validate_pair(pair, profile)
    root = Path(repository_root).expanduser().resolve()
    hidden_spec_path = Path(spec_path).expanduser().resolve()
    upstream_path = Path(upstream_record_path).expanduser().resolve()
    if not root.is_dir() or not hidden_spec_path.is_file() or not upstream_path.is_file():
        raise ValueError("Luanti construction gate inputs must exist")
    spec = _load_yaml(hidden_spec_path)
    spec_errors = validate_evaluation_contract(spec, hidden_spec_path, allow_lazarus=True)
    if spec_errors:
        raise ValueError("Luanti Lazarus oracle is invalid: " + "; ".join(spec_errors))
    record = _load_json(upstream_path)
    provenance_errors = validate_lazarus_trial_provenance(
        record, source_root=pair.fixed.root, harness_root=root
    )
    if provenance_errors:
        raise ValueError(
            "Luanti upstream test provenance is invalid: "
            + "; ".join(provenance_errors)
        )
    definitions = definitions_from_upstream_record(record)
    _validate_registered_test_ids(spec, definitions, profile)
    golden = validate_lazarus_golden_set(
        golden_metadata_path,
        task_id=profile.task_id,
    )
    provenance_verified = not provenance_errors and _provenance_matches_pair(
        pair, record, profile
    )

    sandbox = Path(sandbox_root).expanduser().resolve()
    sandbox.mkdir(parents=True, exist_ok=True)
    registered_ids = (*profile.fail_to_pass, *profile.pass_to_pass)
    fixed_upstream = run_upstream_test_subset(
        pair.fixed.root,
        definitions,
        registered_ids,
        sandbox_root=sandbox,
        harness_root=root,
        trusted_git_dir=pair.fixed.git_metadata,
    )
    buggy_upstream = run_upstream_test_subset(
        pair.buggy.root,
        definitions,
        registered_ids,
        sandbox_root=sandbox,
        harness_root=root,
        trusted_git_dir=pair.buggy.git_metadata,
    )

    with tempfile.TemporaryDirectory(
        prefix=f"{profile.label}-semantic-", dir=sandbox
    ) as temp_dir:
        workspace = Path(temp_dir).resolve()
        fixed_start = _run_semantic_view(
            pair.fixed, _start_view(spec, profile), "fixed-start", workspace
        )
        buggy_start = _run_semantic_view(
            pair.buggy, _start_view(spec, profile), "buggy-start", workspace
        )
        fixed_goal = _run_semantic_view(
            pair.fixed, materialize_lazarus_spec(spec, view="goal"), "fixed-goal", workspace
        )
        buggy_signature = _run_semantic_view(
            pair.buggy, materialize_lazarus_spec(spec, view="defect"), "buggy-signature", workspace
        )
        fixed_signature = _run_semantic_view(
            pair.fixed, materialize_lazarus_spec(spec, view="defect"), "fixed-signature", workspace
        )

    result = evaluate_luanti_gate(
        profile=profile,
        fixed_tests=_outcomes(fixed_upstream),
        buggy_tests=_outcomes(buggy_upstream),
        goldens=golden,
        fixed_start=fixed_start,
        buggy_start=buggy_start,
        fixed_goal=fixed_goal,
        buggy_signature=buggy_signature,
        fixed_signature=fixed_signature,
        provenance_verified=provenance_verified,
    )
    destination = _write_report(
        report_path,
        result=result,
        golden=golden,
        fixed_upstream=fixed_upstream,
        buggy_upstream=buggy_upstream,
        semantic={
            "fixed_start": fixed_start,
            "buggy_start": buggy_start,
            "fixed_goal": fixed_goal,
            "buggy_signature": buggy_signature,
            "fixed_signature": fixed_signature,
        },
        provenance_errors=provenance_errors,
        pair=pair,
    )
    return LuantiConstructionGateRun(
        result=result,
        golden_verification=golden,
        fixed_upstream=fixed_upstream,
        buggy_upstream=buggy_upstream,
        fixed_start=fixed_start,
        buggy_start=buggy_start,
        fixed_goal=fixed_goal,
        buggy_signature=buggy_signature,
        fixed_signature=fixed_signature,
        report_path=destination,
    )


def run_luanti_pr11919_construction_gate(
    pair: LazarusVersionPair, **inputs: Any
) -> LuantiConstructionGateRun:
    """Run the PR #11919 gate through the registered profile."""

    return run_luanti_construction_gate(
        pair, profile=PR11919_GATE_PROFILE, **inputs
    )


def _run_semantic_view(
    artifact: LazarusVersionArtifact,
    spec: Mapping[str, Any],
    label: str,
    workspace: Path,
) -> LuantiSemanticObservation:
    trial = workspace / f"{label}-trial"
    strict_clone_tree(artifact.root, trial)
    output = workspace / f"{label}-output"
    output.mkdir(parents=True, exist_ok=False)
    with _temporary_environment(
        "WARPTEST_LUANTI_TRUSTED_GIT_DIR", str(artifact.git_metadata)
    ):
        with LuantiRuntimeAdapter(
            trial,
            output,
            headless=True,
            visual_capture=False,
            sync_builtin_from_repo=True,
            experiment_seed=0,
        ) as runtime:
            result = runtime.run_checkpoint(copy.deepcopy(dict(spec)), output / f"{label}.yaml")
    checks = result.get("checks") if isinstance(result.get("checks"), list) else []
    statuses = tuple(
        f"{str(check.get('name') or '')}:{str(check.get('status') or '')}"
        for check in checks
        if isinstance(check, Mapping)
    )
    report_value = result.get("report_path")
    report = Path(str(report_value or "")).expanduser().resolve()
    report_sha = _sha256(report) if report.is_file() else None
    primary, invariants = _semantic_group_outcomes(spec, checks)
    target_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and str(check.get("name") or "").startswith("target.validate.")
    ]
    start_ok = result.get("status") == "success" and bool(target_checks) and all(
        check.get("status") == "success" for check in target_checks
    )
    view_checks = [
        check
        for check in checks
        if isinstance(check, Mapping)
        and str(check.get("name") or "").startswith(
            "assertion[luanti_player_view_matches]"
        )
    ]
    view_ok = len(view_checks) == 1 and view_checks[0].get("status") == "success"
    return LuantiSemanticObservation(
        status=str(result.get("status") or "failure"),
        start_ok=start_ok,
        view_ok=view_ok,
        primary_ok=primary,
        invariants_ok=invariants,
        report_sha256=report_sha,
        checks=statuses,
    )


def _start_view(
    spec: Mapping[str, Any], profile: LuantiGateProfile
) -> dict[str, Any]:
    result = copy.deepcopy(dict(spec))
    result["actions"] = []
    result["assertions"] = [copy.deepcopy(dict(profile.start_view_assertion))]
    return result


def _semantic_group_outcomes(
    spec: Mapping[str, Any], checks: list[Any]
) -> tuple[bool, bool]:
    assertions = spec.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        return False, False
    lazarus = ((spec.get("evaluation") or {}).get("lazarus"))
    if not isinstance(lazarus, Mapping):
        return False, False
    invariant_count = len(lazarus.get("invariant_checks") or [])
    primary_count = len(assertions) - invariant_count
    if primary_count < 1 or invariant_count < 1:
        return False, False
    outcomes = [
        check.get("status") == "success"
        for check in checks
        if isinstance(check, Mapping)
        and str(check.get("name") or "").startswith("assertion[")
    ]
    if len(outcomes) != len(assertions):
        return False, False
    return all(outcomes[:primary_count]), all(outcomes[primary_count:])


def _outcomes(result: UpstreamSubsetResult) -> dict[str, bool]:
    return {row.id: row.passed for row in result.results}


def _validate_pair(pair: LazarusVersionPair, profile: LuantiGateProfile) -> None:
    if (
        pair.fixed.task_id != profile.task_id
        or pair.buggy.task_id != profile.task_id
        or pair.fixed.version != "fixed"
        or pair.buggy.version != "buggy"
        or pair.fixed.patch_sha256 != pair.buggy.patch_sha256
    ):
        raise ValueError("Luanti construction gate requires its matched Phase-0 pair")


def _provenance_matches_pair(
    pair: LazarusVersionPair, record: Mapping[str, Any], profile: LuantiGateProfile
) -> bool:
    return (
        record.get("task_id") == profile.task_id
        and record.get("benchmark") == "luanti"
        and record.get("pinned_revision") == pair.fixed.source_revision
        and record.get("patch_sha256") == pair.fixed.patch_sha256
        and record.get("patch_sha256") == pair.buggy.patch_sha256
    )


def _validate_registered_test_ids(
    spec: Mapping[str, Any],
    definitions: Mapping[str, Any],
    profile: LuantiGateProfile,
) -> None:
    lazarus = ((spec.get("evaluation") or {}).get("lazarus"))
    if not isinstance(lazarus, Mapping):
        raise ValueError("Luanti construction gate requires evaluation.lazarus")
    upstream = lazarus.get("upstream_tests")
    if not isinstance(upstream, Mapping):
        raise ValueError("Luanti construction gate requires upstream test ids")
    if tuple(upstream.get("fail_to_pass") or ()) != profile.fail_to_pass:
        raise ValueError("Luanti construction gate F2P registration drift")
    if tuple(upstream.get("pass_to_pass") or ()) != profile.pass_to_pass:
        raise ValueError("Luanti construction gate P2P registration drift")
    if tuple(upstream.get("per_trial_p2p") or ()) != profile.per_trial_p2p:
        raise ValueError("Luanti construction gate per-trial P2P registration drift")
    if not set(profile.fail_to_pass + profile.pass_to_pass) <= set(definitions):
        raise ValueError("Luanti construction gate upstream record is incomplete")


def _write_report(
    path: str | Path,
    *,
    result: ConstructionGateResult,
    golden: LazarusGoldenVerification,
    fixed_upstream: UpstreamSubsetResult,
    buggy_upstream: UpstreamSubsetResult,
    semantic: Mapping[str, LuantiSemanticObservation],
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
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
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
        raise ValueError("Luanti construction gate spec must be a mapping")
    return value


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Luanti construction gate upstream record must be a mapping")
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextmanager
def _temporary_environment(name: str, value: str) -> Iterator[None]:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous
