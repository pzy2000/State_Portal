"""Explicitly relaxed, quality-floor selection of 24 C1-Lazarus tasks."""

from __future__ import annotations

import json
import stat
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from .models import CandidateKey
from .population import (
    V2CandidateEligibility,
    load_v2_candidate_eligibility,
    load_v2_enumeration_audit,
)
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_SCHEMA = "c1-lazarus-curated-catalog-v1"
_SELECTION_MODE = "curated_v1"
_V2_SELECTION_MODE = "curated_v2"
_V2_SEMANTIC_VERSION = "2.0"
_V2_REPETITIONS = 3
_REPOSITORIES = frozenset({"luanti", "blender", "jupyterlab", "openra"})
_TASK_FIELDS = frozenset(
    {
        "task_id",
        "repository_key",
        "change_number",
        "issue_number",
        "defect_locus",
        "public_source_url",
        "public_change_url",
        "mapping_evidence_path",
    }
)
_QUALITY_FLOOR = {
    "public_defect_or_change_record_required": True,
    "real_repair_patch_required": True,
    "registered_fail_to_pass_required": True,
    "registered_pass_to_pass_required": True,
    "construction_revalidation_required": True,
    "current_common_pinned_checkout_required": False,
    "platform_enumerable_issue_relationship_required": False,
}


@dataclass(frozen=True)
class CuratedTask:
    task_id: str
    repository_key: str
    change_number: int
    issue_number: int | None
    defect_locus: str
    public_source_url: str
    public_change_url: str
    source_record_path: str
    repair_patch_path: str
    test_descriptor_path: str
    version_request_path: str
    strict_audit_artifact_id: str
    mapping_evidence_path: str | None = None


def load_curated_catalog(
    catalog_path: str | Path,
    *,
    workspace_root: str | Path,
) -> tuple[CuratedTask, ...]:
    """Load the relaxed catalog and mechanically enforce its quality floor."""

    workspace = _safe_directory(Path(workspace_root))
    source = _safe_file(Path(catalog_path), workspace)
    document = yaml.safe_load(source.read_text(encoding="utf-8"))
    if type(document) is not dict or set(document) != {
        "schema",
        "schema_version",
        "selection_mode",
        "population",
        "quality_floor",
        "candidates",
    }:
        raise ValueError("curated relaxed catalog schema is invalid")
    if (
        document["schema"] != _SCHEMA
        or document["schema_version"] != 1
        or document["selection_mode"] != _SELECTION_MODE
        or document["quality_floor"] != _QUALITY_FLOOR
    ):
        raise ValueError("curated relaxed catalog policy is invalid")
    raw_candidates = document["candidates"]
    if type(raw_candidates) is not list or len(raw_candidates) != 24:
        raise ValueError("curated relaxed catalog must contain exactly 24 tasks")

    tasks: list[CuratedTask] = []
    task_ids: set[str] = set()
    identities: set[tuple[str, int]] = set()
    for raw in raw_candidates:
        if type(raw) is not dict or set(raw) != _TASK_FIELDS:
            raise ValueError("curated relaxed task schema is invalid")
        task_id = raw["task_id"]
        repository = raw["repository_key"]
        change_number = raw["change_number"]
        issue_number = raw["issue_number"]
        locus = raw["defect_locus"]
        source_url = raw["public_source_url"]
        change_url = raw["public_change_url"]
        mapping_path = raw["mapping_evidence_path"]
        if type(task_id) is not str or not task_id.startswith("lz-"):
            raise ValueError("curated relaxed task ID is invalid")
        if type(repository) is not str or repository not in _REPOSITORIES:
            raise ValueError("curated relaxed repository is invalid")
        if type(change_number) is not int or change_number <= 0:
            raise ValueError("curated relaxed change number is invalid")
        if issue_number is not None and (
            type(issue_number) is not int or issue_number <= 0
        ):
            raise ValueError("curated relaxed issue number is invalid")
        if locus not in {"at_goal", "on_path"}:
            raise ValueError("curated relaxed defect locus is invalid")
        if not _public_url(source_url) or not _public_url(change_url):
            raise ValueError("curated relaxed public source is invalid")
        identity = (repository, change_number)
        if task_id in task_ids or identity in identities:
            raise ValueError("curated relaxed task is duplicated")
        task_ids.add(task_id)
        identities.add(identity)

        base = Path("benchmarks/p5/lazarus/defects") / task_id
        source_record = _safe_file(workspace / base / "upstream.json", workspace)
        repair_patch = _safe_file(workspace / base / "revert.patch", workspace)
        test_descriptor = _safe_file(
            workspace / "benchmarks/p5/lazarus/authoring" / f"{task_id}.tests.json",
            workspace,
        )
        version_request = _safe_file(
            workspace
            / "benchmarks/p5/lazarus/authoring"
            / f"{task_id}.version-request.json",
            workspace,
        )
        _validate_source_record(
            source_record,
            task_id=task_id,
            repository=repository,
            source_url=source_url,
        )
        _validate_repair_patch(repair_patch)
        _validate_test_descriptor(test_descriptor)
        _validate_version_request(version_request, task_id)

        if mapping_path is not None:
            if type(mapping_path) is not str:
                raise ValueError("curated relaxed mapping evidence path is invalid")
            mapping_file = _safe_file(workspace / mapping_path, workspace)
            if not _mapping_is_supported(
                mapping_file,
                repository=repository,
                issue_number=issue_number,
                change_number=change_number,
            ):
                raise ValueError("curated relaxed mapping is unsupported")
        task = CuratedTask(
            task_id=task_id,
            repository_key=repository,
            change_number=change_number,
            issue_number=issue_number,
            defect_locus=locus,
            public_source_url=source_url,
            public_change_url=change_url,
            source_record_path=source_record.relative_to(workspace).as_posix(),
            repair_patch_path=repair_patch.relative_to(workspace).as_posix(),
            test_descriptor_path=test_descriptor.relative_to(workspace).as_posix(),
            version_request_path=version_request.relative_to(workspace).as_posix(),
            strict_audit_artifact_id=f"old24_audit/{task_id}/current/v1",
            mapping_evidence_path=mapping_path,
        )
        tasks.append(task)

    population = {
        "task_count": 24,
        "repositories": dict(Counter(task.repository_key for task in tasks)),
        "defect_locus": dict(Counter(task.defect_locus for task in tasks)),
    }
    if document["population"] != population:
        raise ValueError("curated relaxed population declaration drifted")
    if population["defect_locus"] != {"at_goal": 16, "on_path": 8}:
        raise ValueError("curated relaxed population must preserve the 16/8 quota")
    return tuple(tasks)


def write_curated_selection(
    artifact_root: str | Path,
    *,
    catalog_path: str | Path,
    workspace_root: str | Path,
    strict_selection_artifact_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Select 24 curated tasks without rewriting the strict screening result."""

    tasks = load_curated_catalog(
        catalog_path,
        workspace_root=workspace_root,
    )
    strict_selection = load_stable_artifact(
        artifact_root,
        artifact_id=strict_selection_artifact_id,
        artifact_kind="population_selection",
        semantic_version="1.0",
    )
    if (
        strict_selection.payload.get("selection_status") != "insufficient_population"
        or strict_selection.payload.get("qualified_count") != 0
        or strict_selection.payload.get("selected_candidates") != []
        or strict_selection.payload.get("complete") is not True
    ):
        raise RuntimeError(
            "strict population result is not the recorded zero-task stop"
        )

    audits = []
    for task in tasks:
        audit = load_stable_artifact(
            artifact_root,
            artifact_id=task.strict_audit_artifact_id,
            artifact_kind="old24_audit",
            semantic_version="1.0",
        )
        payload = audit.payload
        if (
            payload.get("task_label") != task.task_id
            or payload.get("repository_key") != task.repository_key
            or payload.get("inherited_conclusions") is not False
        ):
            raise RuntimeError("curated task strict audit binding is invalid")
        if payload.get("mapping_state") == "mapped":
            if payload.get("change_number") != task.change_number:
                raise RuntimeError("curated task strict mapping changed")
        elif not (
            payload.get("mapping_state") == "unresolved"
            and task.mapping_evidence_path is not None
        ):
            raise RuntimeError("curated task has no strict audit or mapping resolution")
        audits.append(audit)

    payload = {
        "selection_mode": _SELECTION_MODE,
        "selection_status": "selected_for_construction_revalidation",
        "selected_task_count": len(tasks),
        "defect_locus": {"at_goal": 16, "on_path": 8},
        "repositories": dict(Counter(task.repository_key for task in tasks)),
        "strict_selection_status": strict_selection.payload["selection_status"],
        "strict_population_result_preserved": True,
        "unbiased_population_selection": False,
        "construction_revalidation_required": True,
        "confirmatory_ready": False,
        "paid_actions_authorized": False,
        "formal_execution_authorized": False,
        "quality_floor": dict(_QUALITY_FLOOR),
        "tasks": [_task_value(task) for task in tasks],
        "complete": True,
    }
    artifact_id = "curated_selection/all/current/v1"
    inputs = (strict_selection.reference, *(audit.reference for audit in audits))
    write_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="curated_selection",
        semantic_version="1.0",
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    selected = load_stable_artifact(
        artifact_root,
        artifact_id=artifact_id,
        artifact_kind="curated_selection",
        semantic_version="1.0",
    )
    if selected.inputs != inputs or selected.payload != payload:
        raise RuntimeError("curated relaxed selection reload drift")
    return selected


def write_curated_construction_transition_receipt_v2(
    artifact_root: str | Path,
    *,
    task_id: str,
    catalog_path: str | Path,
    workspace_root: str | Path,
    original_selection_artifact_id: str,
    original_revalidation_artifact_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Bridge one historical revalidation only after normal v2 admission exists.

    This is intentionally the sole v1-reading path in the v2 curated flow.
    It does not treat a task label, an old audit, or a historical aggregate as
    admission.  The candidate must first have a complete v2 source, patch,
    and eligibility chain.  The old aggregate is then accepted only when its
    original selection reference and all three typed repetitions prove the
    exact fixed/buggy F2P/P2P truth table.
    """

    task = _load_catalog_task(task_id, catalog_path, workspace_root)
    root = Path(artifact_root)
    candidate = CandidateKey(task.repository_key, task.change_number)
    eligibility = load_v2_candidate_eligibility(root, candidate)
    audit = load_v2_enumeration_audit(root, candidate)
    original_selection = _load_legacy_selection(
        root,
        original_selection_artifact_id,
        task,
    )
    revalidation, attempts, truth_table = _load_legacy_revalidation(
        root,
        task,
        original_selection,
        original_revalidation_artifact_id,
    )
    inputs = _v2_chain_inputs(eligibility, audit) + (
        original_selection.reference,
        revalidation.reference,
        *(attempt.reference for attempt in attempts),
    )
    payload = {
        "task_id": task.task_id,
        "candidate": candidate.to_value(),
        "defect_locus": task.defect_locus,
        "transition_status": "reused_qualified_revalidation",
        "normal_v2_chain_required": True,
        "required_repetitions": _V2_REPETITIONS,
        "completed_repetitions": _V2_REPETITIONS,
        "truth_table": truth_table,
        "original_selection_artifact_id": original_selection.reference.artifact_id,
        "original_revalidation_artifact_id": revalidation.reference.artifact_id,
        "complete": True,
    }
    artifact_id = _transition_id(task)
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="curated_construction_transition",
        semantic_version=_V2_SEMANTIC_VERSION,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    transition = _load_v2_transition_receipt(
        root,
        task,
        eligibility,
        audit,
    )
    if transition.inputs != inputs or transition.payload != payload:
        raise RuntimeError("v2 construction transition receipt reload drift")
    return transition


def write_curated_task_admission_v2(
    artifact_root: str | Path,
    *,
    task_id: str,
    catalog_path: str | Path,
    workspace_root: str | Path,
    transition_artifact_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Persist one v2-only curated admission after all normal gates reload."""

    task = _load_catalog_task(task_id, catalog_path, workspace_root)
    root = Path(artifact_root)
    candidate = CandidateKey(task.repository_key, task.change_number)
    eligibility = load_v2_candidate_eligibility(root, candidate)
    audit = load_v2_enumeration_audit(root, candidate)
    if transition_artifact_id != _transition_id(task):
        raise RuntimeError("v2 construction transition identity is invalid")
    transition = _load_v2_transition_receipt(root, task, eligibility, audit)
    inputs = (*_v2_chain_inputs(eligibility, audit), transition.reference)
    payload = {
        "task_id": task.task_id,
        "candidate": candidate.to_value(),
        "defect_locus": task.defect_locus,
        "admission_state": "eligible",
        "normal_v2_chain_required": True,
        "construction_transition_artifact_id": transition.reference.artifact_id,
        "complete": True,
    }
    artifact_id = _admission_id(task)
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="curated_task_admission",
        semantic_version=_V2_SEMANTIC_VERSION,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    admission = _load_v2_admission(root, task, eligibility, audit, transition)
    if admission.inputs != inputs or admission.payload != payload:
        raise RuntimeError("v2 curated task admission reload drift")
    return admission


def write_curated_selection_v2(
    artifact_root: str | Path,
    *,
    catalog_path: str | Path,
    workspace_root: str | Path,
    screening_population_artifact_id: str,
    coverage_artifact_id: str,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Select only tasks independently admitted by the normal v2 pipeline.

    Mapped or unresolved ``old24_audit`` labels are deliberately not read by
    this writer.  A catalog row is only a declaration; it becomes admissible
    after a v2 population record, v2 coverage/audit closure, a normal v2
    source-to-eligibility DAG, and the narrowly validated construction
    transition receipt all reload successfully.
    """

    if screening_population_artifact_id != "screening_population/all/current/v2":
        raise ValueError("v2 screening population identity is invalid")
    if coverage_artifact_id != "selection_coverage/all/current/v2":
        raise ValueError("v2 selection coverage identity is invalid")
    tasks = load_curated_catalog(
        catalog_path,
        workspace_root=workspace_root,
    )
    root = Path(artifact_root)
    population = _load_v2_artifact(
        root,
        artifact_id=screening_population_artifact_id,
        artifact_kind="screening_population",
        label="v2 screening population",
    )
    coverage = _load_v2_artifact(
        root,
        artifact_id=coverage_artifact_id,
        artifact_kind="selection_coverage",
        label="v2 selection coverage",
    )
    _validate_v2_population_coverage(population, coverage)
    population_records = _v2_population_records(population)
    admissions: list[StableArtifact] = []
    for task in tasks:
        candidate = CandidateKey(task.repository_key, task.change_number)
        eligibility = load_v2_candidate_eligibility(root, candidate)
        audit = load_v2_enumeration_audit(root, candidate)
        _require_v2_population_record(
            population_records,
            candidate,
            eligibility.preannotation_checkpoint.reference,
        )
        if audit.reference not in coverage.inputs:
            raise RuntimeError("v2 selection coverage omits a candidate audit")
        transition = _load_v2_transition_receipt(root, task, eligibility, audit)
        admissions.append(
            _load_v2_admission(root, task, eligibility, audit, transition)
        )
    inputs = (
        population.reference,
        coverage.reference,
        *(item.reference for item in admissions),
    )
    payload = {
        "selection_mode": _V2_SELECTION_MODE,
        "selection_status": "selected_for_preannotation_readiness",
        "selected_task_count": len(tasks),
        "defect_locus": {"at_goal": 16, "on_path": 8},
        "repositories": dict(Counter(task.repository_key for task in tasks)),
        "normal_v2_admission_required": True,
        "construction_transition_required": True,
        "confirmatory_ready": False,
        "paid_actions_authorized": False,
        "formal_execution_authorized": False,
        "quality_floor": dict(_QUALITY_FLOOR),
        "tasks": [_v2_task_value(task) for task in tasks],
        "complete": True,
    }
    artifact_id = "curated_selection/all/current/v2"
    write_stable_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="curated_selection",
        semantic_version=_V2_SEMANTIC_VERSION,
        created_at=_now(),
        inputs=inputs,
        payload=payload,
        authorization=authorization,
    )
    selected = _load_v2_artifact(
        root,
        artifact_id=artifact_id,
        artifact_kind="curated_selection",
        label="v2 curated selection",
    )
    if selected.inputs != inputs or selected.payload != payload:
        raise RuntimeError("v2 curated selection reload drift")
    return selected


def _load_catalog_task(
    task_id: str,
    catalog_path: str | Path,
    workspace_root: str | Path,
) -> CuratedTask:
    if type(task_id) is not str or not task_id:
        raise ValueError("v2 curated task ID is invalid")
    matches = tuple(
        task
        for task in load_curated_catalog(
            catalog_path,
            workspace_root=workspace_root,
        )
        if task.task_id == task_id
    )
    if len(matches) != 1:
        raise RuntimeError("v2 curated task is not in the catalog")
    return matches[0]


def _load_legacy_selection(
    root: Path,
    artifact_id: str,
    task: CuratedTask,
) -> StableArtifact:
    if type(artifact_id) is not str or not artifact_id.endswith("/v1"):
        raise ValueError("legacy curated selection identity is invalid")
    try:
        selection = load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind="curated_selection",
            semantic_version="1.0",
        )
    except RuntimeError as error:
        raise RuntimeError("legacy curated selection is missing or invalid") from error
    rows = selection.payload.get("tasks")
    if (
        selection.payload.get("selection_mode") != _SELECTION_MODE
        or selection.payload.get("complete") is not True
        or type(rows) is not list
    ):
        raise RuntimeError("legacy curated selection is invalid")
    candidate = CandidateKey(task.repository_key, task.change_number).to_value()
    matches = [
        row
        for row in rows
        if type(row) is dict
        and row.get("task_id") == task.task_id
        and row.get("candidate") == candidate
        and row.get("defect_locus") == task.defect_locus
    ]
    if len(matches) != 1:
        raise RuntimeError("legacy curated selection does not bind the task")
    return selection


def _load_legacy_revalidation(
    root: Path,
    task: CuratedTask,
    selection: StableArtifact,
    artifact_id: str,
) -> tuple[StableArtifact, tuple[StableArtifact, ...], dict[str, dict[str, bool]]]:
    if artifact_id != _legacy_revalidation_id(task):
        raise RuntimeError("legacy curated revalidation identity is invalid")
    try:
        aggregate = load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind="curated_task_revalidation",
            semantic_version="2.0",
        )
    except RuntimeError as error:
        raise RuntimeError(
            "legacy curated revalidation is missing or invalid"
        ) from error
    attempts = tuple(
        _load_legacy_revalidation_attempt(root, task, selection, repetition)
        for repetition in range(1, _V2_REPETITIONS + 1)
    )
    truth_tables = tuple(
        _legacy_attempt_truth_table(attempt, task) for attempt in attempts
    )
    if len(set(_freeze_truth_table(value) for value in truth_tables)) != 1:
        raise RuntimeError("legacy curated revalidation truth table is unstable")
    truth_table = truth_tables[0]
    candidate = CandidateKey(task.repository_key, task.change_number).to_value()
    expected_inputs = (
        selection.reference,
        *(attempt.reference for attempt in attempts),
    )
    payload = aggregate.payload
    first_attempt = attempts[0].payload
    if (
        aggregate.inputs != expected_inputs
        or payload.get("task_id") != task.task_id
        or payload.get("candidate") != candidate
        or payload.get("defect_locus") != task.defect_locus
        or payload.get("outcome") != "qualified"
        or payload.get("complete") is not True
        or payload.get("qualified") is not True
        or payload.get("fresh_revalidation") is not True
        or payload.get("historical_result_inherited") is not False
        or payload.get("required_repetitions") != _V2_REPETITIONS
        or payload.get("completed_repetitions") != _V2_REPETITIONS
        or payload.get("fixed_tests") != first_attempt.get("fixed_tests")
        or payload.get("buggy_tests") != first_attempt.get("buggy_tests")
        or not _truth_table_matches(
            payload.get("fixed_tests"), payload.get("buggy_tests"), truth_table
        )
    ):
        raise RuntimeError("legacy curated revalidation binding is invalid")
    repetitions = payload.get("repetitions")
    if type(repetitions) is not list or len(repetitions) != _V2_REPETITIONS:
        raise RuntimeError("legacy curated revalidation repetitions are invalid")
    for repetition, attempt in enumerate(attempts, start=1):
        row = repetitions[repetition - 1]
        if (
            type(row) is not dict
            or row.get("repetition") != repetition
            or row.get("outcome") != "qualified"
            or row.get("complete") is not True
            or not _truth_table_matches(
                row.get("fixed_tests"), row.get("buggy_tests"), truth_table
            )
            or row.get("fixed_tests") != attempt.payload.get("fixed_tests")
            or row.get("buggy_tests") != attempt.payload.get("buggy_tests")
            or attempt.payload.get("repetition") != repetition
        ):
            raise RuntimeError("legacy curated revalidation repetition drift")
    return aggregate, attempts, truth_table


def _load_legacy_revalidation_attempt(
    root: Path,
    task: CuratedTask,
    selection: StableArtifact,
    repetition: int,
) -> StableArtifact:
    try:
        attempt = load_stable_artifact(
            root,
            artifact_id=(
                f"curated_task_revalidation/{task.repository_key}/"
                f"{task.change_number}/construction/repetition-{repetition}"
            ),
            artifact_kind="curated_construction_repetition",
            semantic_version="1.0",
        )
    except RuntimeError as error:
        raise RuntimeError(
            "legacy curated revalidation repetition is missing"
        ) from error
    payload = attempt.payload
    candidate = CandidateKey(task.repository_key, task.change_number).to_value()
    if (
        attempt.inputs != (selection.reference,)
        or payload.get("task_id") != task.task_id
        or payload.get("candidate") != candidate
        or payload.get("defect_locus") != task.defect_locus
        or payload.get("repetition") != repetition
        or payload.get("outcome") != "qualified"
        or payload.get("complete") is not True
        or payload.get("fresh_revalidation") is not True
        or payload.get("historical_result_inherited") is not False
    ):
        raise RuntimeError("legacy curated revalidation repetition is invalid")
    return attempt


def _legacy_attempt_truth_table(
    attempt: StableArtifact,
    task: CuratedTask,
) -> dict[str, dict[str, bool]]:
    payload = attempt.payload
    fixed = _bool_test_table(payload.get("fixed_tests"), "fixed")
    buggy = _bool_test_table(payload.get("buggy_tests"), "buggy")
    if set(fixed) != set(buggy):
        raise RuntimeError("legacy curated revalidation test identities drift")
    if set(fixed) == {"f2p", "p2p"}:
        f2p = ("f2p",)
        p2p = ("p2p",)
    else:
        gate = payload.get("registered_gate")
        if type(gate) is not dict or gate.get("task_id") != task.task_id:
            raise RuntimeError("legacy curated revalidation truth table is unavailable")
        f2p = _registered_truth_table_ids(gate.get("fail_to_pass"), "F2P")
        p2p = _registered_truth_table_ids(gate.get("pass_to_pass"), "P2P")
        if set(f2p) | set(p2p) != set(fixed) or set(f2p) & set(p2p):
            raise RuntimeError("legacy curated revalidation truth table is invalid")
    if (
        not all(fixed[test_id] for test_id in (*f2p, *p2p))
        or not all(not buggy[test_id] for test_id in f2p)
        or not all(buggy[test_id] for test_id in p2p)
    ):
        raise RuntimeError("legacy curated revalidation truth table is invalid")
    return {
        "fixed": {"f2p": True, "p2p": True},
        "buggy": {"f2p": False, "p2p": True},
    }


def _bool_test_table(value: Any, label: str) -> dict[str, bool]:
    if (
        type(value) is not dict
        or not value
        or any(
            type(key) is not str or not key or type(result) is not bool
            for key, result in value.items()
        )
    ):
        raise RuntimeError(f"legacy curated {label} test table is invalid")
    return dict(value)


def _registered_truth_table_ids(value: Any, label: str) -> tuple[str, ...]:
    if (
        type(value) is not list
        or not value
        or any(type(item) is not str or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise RuntimeError(f"legacy curated {label} test IDs are invalid")
    return tuple(value)


def _freeze_truth_table(
    value: dict[str, dict[str, bool]],
) -> tuple[tuple[str, bool], ...]:
    return tuple(
        (f"{version}:{name}", passed)
        for version, table in sorted(value.items())
        for name, passed in sorted(table.items())
    )


def _truth_table_matches(
    fixed_value: Any,
    buggy_value: Any,
    truth_table: dict[str, dict[str, bool]],
) -> bool:
    try:
        fixed = _bool_test_table(fixed_value, "fixed")
        buggy = _bool_test_table(buggy_value, "buggy")
    except RuntimeError:
        return False
    if set(fixed) != set(buggy):
        return False
    if set(fixed) == {"f2p", "p2p"}:
        return (
            fixed["f2p"] is truth_table["fixed"]["f2p"]
            and fixed["p2p"] is truth_table["fixed"]["p2p"]
            and buggy["f2p"] is truth_table["buggy"]["f2p"]
            and buggy["p2p"] is truth_table["buggy"]["p2p"]
        )
    return True


def _v2_chain_inputs(
    eligibility: V2CandidateEligibility,
    audit: StableArtifact,
) -> tuple[ArtifactReference, ...]:
    inputs = (
        eligibility.enumeration.reference,
        audit.reference,
        eligibility.source_packet.reference,
        eligibility.test_patch.reference,
        eligibility.screening_checkpoint.reference,
        *(stage.reference for stage in eligibility.stages),
        eligibility.preannotation_checkpoint.reference,
    )
    if any(reference.semantic_version != _V2_SEMANTIC_VERSION for reference in inputs):
        raise RuntimeError("v2 candidate chain contains a non-v2 reference")
    return inputs


def _load_v2_transition_receipt(
    root: Path,
    task: CuratedTask,
    eligibility: V2CandidateEligibility,
    audit: StableArtifact,
) -> StableArtifact:
    artifact = _load_v2_artifact(
        root,
        artifact_id=_transition_id(task),
        artifact_kind="curated_construction_transition",
        label="v2 construction transition receipt",
    )
    payload = artifact.payload
    candidate = CandidateKey(task.repository_key, task.change_number).to_value()
    selection_id = payload.get("original_selection_artifact_id")
    revalidation_id = payload.get("original_revalidation_artifact_id")
    if (
        payload.get("task_id") != task.task_id
        or payload.get("candidate") != candidate
        or payload.get("defect_locus") != task.defect_locus
        or payload.get("transition_status") != "reused_qualified_revalidation"
        or payload.get("normal_v2_chain_required") is not True
        or payload.get("required_repetitions") != _V2_REPETITIONS
        or payload.get("completed_repetitions") != _V2_REPETITIONS
        or payload.get("complete") is not True
        or type(selection_id) is not str
        or type(revalidation_id) is not str
        or payload.get("truth_table")
        != {
            "fixed": {"f2p": True, "p2p": True},
            "buggy": {"f2p": False, "p2p": True},
        }
    ):
        raise RuntimeError("v2 construction transition receipt is invalid")
    selection = _load_legacy_selection(root, selection_id, task)
    revalidation, attempts, truth_table = _load_legacy_revalidation(
        root,
        task,
        selection,
        revalidation_id,
    )
    expected_inputs = _v2_chain_inputs(eligibility, audit) + (
        selection.reference,
        revalidation.reference,
        *(attempt.reference for attempt in attempts),
    )
    if artifact.inputs != expected_inputs or payload["truth_table"] != truth_table:
        raise RuntimeError("v2 construction transition receipt binding is invalid")
    return artifact


def _load_v2_admission(
    root: Path,
    task: CuratedTask,
    eligibility: V2CandidateEligibility,
    audit: StableArtifact,
    transition: StableArtifact,
) -> StableArtifact:
    artifact = _load_v2_artifact(
        root,
        artifact_id=_admission_id(task),
        artifact_kind="curated_task_admission",
        label="v2 curated task admission",
    )
    candidate = CandidateKey(task.repository_key, task.change_number).to_value()
    expected_inputs = (*_v2_chain_inputs(eligibility, audit), transition.reference)
    if (
        artifact.inputs != expected_inputs
        or artifact.payload.get("task_id") != task.task_id
        or artifact.payload.get("candidate") != candidate
        or artifact.payload.get("defect_locus") != task.defect_locus
        or artifact.payload.get("admission_state") != "eligible"
        or artifact.payload.get("normal_v2_chain_required") is not True
        or artifact.payload.get("construction_transition_artifact_id")
        != transition.reference.artifact_id
        or artifact.payload.get("complete") is not True
    ):
        raise RuntimeError("v2 curated task admission is invalid")
    return artifact


def _validate_v2_population_coverage(
    population: StableArtifact,
    coverage: StableArtifact,
) -> None:
    if (
        population.payload.get("selection_epoch") != "v2"
        or population.payload.get("complete") is not True
        or coverage.payload.get("selection_epoch") != "v2"
        or coverage.payload.get("complete") is not True
        or coverage.payload.get("screening_population_artifact_id")
        != population.reference.artifact_id
        or population.reference not in coverage.inputs
        or any(
            reference.semantic_version != _V2_SEMANTIC_VERSION
            for reference in coverage.inputs
        )
    ):
        raise RuntimeError("v2 population/coverage closure is invalid")


def _v2_population_records(
    population: StableArtifact,
) -> dict[tuple[str, int], dict[str, object]]:
    records = population.payload.get("records")
    if type(records) is not list:
        raise RuntimeError("v2 screening population records are invalid")
    result: dict[tuple[str, int], dict[str, object]] = {}
    for row in records:
        if type(row) is not dict:
            raise RuntimeError("v2 screening population record is invalid")
        try:
            candidate = CandidateKey.from_value(row.get("candidate"))
        except ValueError as error:
            raise RuntimeError(
                "v2 screening population candidate is invalid"
            ) from error
        key = (candidate.repository_key, candidate.change_number)
        if key in result:
            raise RuntimeError("v2 screening population candidate is duplicated")
        if (
            type(row.get("outcome")) is not str
            or type(row.get("terminal_artifact_id")) is not str
            or type(row.get("terminal_artifact_kind")) is not str
        ):
            raise RuntimeError("v2 screening population record is invalid")
        result[key] = row
    return result


def _require_v2_population_record(
    records: dict[tuple[str, int], dict[str, object]],
    candidate: CandidateKey,
    terminal: ArtifactReference,
) -> None:
    row = records.get((candidate.repository_key, candidate.change_number))
    if (
        row is None
        or row.get("outcome") != "qualified"
        or row.get("terminal_artifact_id") != terminal.artifact_id
        or row.get("terminal_artifact_kind") != terminal.artifact_kind
    ):
        raise RuntimeError("v2 screening population does not admit the candidate")


def _load_v2_artifact(
    root: Path,
    *,
    artifact_id: str,
    artifact_kind: str,
    label: str,
) -> StableArtifact:
    try:
        return load_stable_artifact(
            root,
            artifact_id=artifact_id,
            artifact_kind=artifact_kind,
            semantic_version=_V2_SEMANTIC_VERSION,
        )
    except RuntimeError as error:
        raise RuntimeError(f"{label} is missing or invalid") from error


def _transition_id(task: CuratedTask) -> str:
    return (
        f"curated_construction_transition/{task.repository_key}/"
        f"{task.change_number}/current/v2"
    )


def _admission_id(task: CuratedTask) -> str:
    return (
        f"curated_task_admission/{task.repository_key}/{task.change_number}/current/v2"
    )


def _legacy_revalidation_id(task: CuratedTask) -> str:
    return (
        f"curated_task_revalidation/{task.repository_key}/"
        f"{task.change_number}/construction"
    )


def _task_value(task: CuratedTask) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "candidate": {
            "repository_key": task.repository_key,
            "change_number": task.change_number,
        },
        "issue_number": task.issue_number,
        "defect_locus": task.defect_locus,
        "public_source_url": task.public_source_url,
        "public_change_url": task.public_change_url,
        "source_record_path": task.source_record_path,
        "repair_patch_path": task.repair_patch_path,
        "test_descriptor_path": task.test_descriptor_path,
        "version_request_path": task.version_request_path,
        "mapping_evidence_path": task.mapping_evidence_path,
        "strict_audit_artifact_id": task.strict_audit_artifact_id,
    }


def _v2_task_value(task: CuratedTask) -> dict[str, Any]:
    """Project catalog metadata without carrying the legacy audit locator."""

    value = _task_value(task)
    value.pop("strict_audit_artifact_id")
    return value


def _validate_source_record(
    path: Path,
    *,
    task_id: str,
    repository: str,
    source_url: str,
) -> None:
    value = _strict_json(path)
    if value.get("task_id") != task_id or value.get("benchmark") != repository:
        raise ValueError("curated source record identity is invalid")
    urls = {
        part.get("url")
        for key in ("issue", "pull_request")
        if type(part := value.get(key)) is dict
    }
    if source_url not in urls:
        raise ValueError("curated public source is not frozen in the source record")
    if type(value.get("touched_files")) is not list or not value["touched_files"]:
        raise ValueError("curated source record has no repair files")


def _validate_repair_patch(path: Path) -> None:
    raw = path.read_bytes()
    if not raw or b"diff --git " not in raw or b"@@" not in raw:
        raise ValueError("curated repair patch is invalid")


def _validate_test_descriptor(path: Path) -> None:
    value = _strict_json(path)
    tests = value.get("tests")
    if type(tests) is not list or not tests:
        raise ValueError("curated test descriptor is empty")
    kinds = set()
    identifiers = set()
    for test in tests:
        if type(test) is not dict:
            raise ValueError("curated registered test is invalid")
        identifier = test.get("id")
        kind = test.get("kind")
        if type(identifier) is not str or not identifier or identifier in identifiers:
            raise ValueError("curated registered test ID is invalid")
        if kind not in {"fail_to_pass", "pass_to_pass", "per_trial_p2p"}:
            raise ValueError("curated registered test kind is invalid")
        source_case = test.get("source_case")
        if not (
            type(source_case) is str
            and source_case
            or (
                test.get("origin") == "issue_derived_gui_differential"
                and type(test.get("provenance")) is str
                and bool(test["provenance"])
                and type(test.get("argv")) is list
                and bool(test["argv"])
            )
        ):
            raise ValueError("curated registered test has no executable selector")
        identifiers.add(identifier)
        kinds.add(kind)
    if "fail_to_pass" not in kinds or "pass_to_pass" not in kinds:
        raise ValueError("curated task lacks an F2P/P2P quality floor")


def _validate_version_request(path: Path, task_id: str) -> None:
    value = _strict_json(path)
    if value.get("task_id") != task_id:
        raise ValueError("curated version request identity is invalid")
    if type(value.get("revert_patch")) is not str or not value["revert_patch"]:
        raise ValueError("curated version request has no repair patch")


def _mapping_is_supported(
    path: Path,
    *,
    repository: str,
    issue_number: int | None,
    change_number: int,
) -> bool:
    document = _strict_json(path)

    def visit(value: Any) -> bool:
        if type(value) is dict:
            references = value.get("references")
            if (
                value.get("benchmark") == repository
                and type(references) is dict
                and issue_number in references.get("issue_numbers", [])
                and change_number in references.get("pr_numbers", [])
                and value.get("mechanical_patch_gate") is True
                and value.get("reverse_check_passed") is True
            ):
                return True
            return any(visit(child) for child in value.values())
        if type(value) is list:
            return any(visit(child) for child in value)
        return False

    return visit(document)


def _strict_json(path: Path) -> dict[str, Any]:
    def reject_duplicate(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("curated evidence contains duplicate JSON keys")
            result[key] = value
        return result

    value = json.loads(
        path.read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicate,
        parse_constant=lambda token: (_ for _ in ()).throw(
            ValueError(f"invalid JSON constant: {token}")
        ),
    )
    if type(value) is not dict:
        raise ValueError("curated evidence root is invalid")
    return value


def _safe_directory(path: Path) -> Path:
    absolute = path.absolute()
    info = absolute.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise ValueError("curated workspace root is unsafe")
    return absolute.resolve(strict=True)


def _safe_file(path: Path, workspace: Path) -> Path:
    absolute = path.absolute()
    resolved = absolute.resolve(strict=True)
    if workspace != resolved and workspace not in resolved.parents:
        raise ValueError("curated evidence escapes the workspace")
    info = absolute.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValueError("curated evidence file is unsafe")
    return resolved


def _public_url(value: Any) -> bool:
    return type(value) is str and value.startswith("https://") and "/" in value[8:]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "CuratedTask",
    "load_curated_catalog",
    "write_curated_construction_transition_receipt_v2",
    "write_curated_selection",
    "write_curated_selection_v2",
    "write_curated_task_admission_v2",
]
