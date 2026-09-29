"""Emit executable Luanti lazarus task specs for the frozen catalog rows.

Each spec restates the contract its closure already admitted: the registered start,
the observation surface, the visual oracle, and a lazarus block whose goal and
defect checks come from the candidate's own Gate 10 evidence.  Nothing here invents
an expectation; :mod:`warptest.lazarus_luanti_states` recovers the pair and this
module refuses to write a spec for a task that has none.

A spec also records which observation source resolves its checks, because only some
candidates committed a replay that can be re-executed offline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

import yaml

from ..lazarus_luanti_probe import (
    CHECKPOINT_STRATEGY,
    FROZEN_SOURCE,
    LIVE_SOURCE,
    LuantiProbeBinding,
    build_luanti_bindings,
    evaluate_luanti_lazarus_audit,
    evaluate_luanti_lazarus_checks,
    luanti_lazarus_defect_signature,
    luanti_lazarus_goal_checks,
    run_luanti_lazarus_probe,
)
from ..lazarus_luanti_states import PROBE_ERROR_CHECK, luanti_truth_table
from ..spec_schema import ensure_valid_spec_schema
from .c1_lazarus_golden_publication import _TASK_IDS
from .stable_protocol import (
    ArtifactReference,
    AuthorizationRecord,
    StableArtifact,
    load_stable_artifact,
    write_stable_artifact,
)


_TIMEOUT_MS = 2_400_000
_TAGS = ("c1", "lazarus", "luanti", "production")
_SPECS = ArtifactReference(
    "c1_lazarus_luanti_task_specs/lazarus/v2-roster/current-v1",
    "c1_lazarus_luanti_task_specs",
    "1.0",
)
_VERIFICATION = ArtifactReference(
    "c1_lazarus_luanti_spec_verification/lazarus/v2-roster/current-v1",
    "c1_lazarus_luanti_spec_verification",
    "1.0",
)


@dataclass(frozen=True)
class WrittenLuantiSpec:
    task_id: str
    change_number: int
    path: Path
    source: str
    evidence_layout: str


def luanti_task_ids() -> dict[int, str]:
    """Return the frozen Luanti change numbers mapped to their task ids."""

    return {
        change_number: task_id
        for (repository_key, change_number), task_id in _TASK_IDS.items()
        if repository_key == "luanti"
    }


def write_luanti_task_specs(
    artifact_root: str | Path,
    *,
    repository_root: str | Path,
    authorization: AuthorizationRecord,
) -> StableArtifact:
    """Write one spec per frozen Luanti row and validate every one of them."""

    _require_scope(authorization, "infrastructure-audit")
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    bindings = build_luanti_bindings(root, task_ids=luanti_task_ids())
    written: list[WrittenLuantiSpec] = []
    for task_id, binding in sorted(bindings.items()):
        manifest_path = repo / "specs/luanti/lazarus" / f"{task_id}.goldens.yaml"
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        document = _spec_document(binding=binding, manifest=manifest)
        path = repo / "specs/luanti/lazarus" / f"{task_id}.yaml"
        path.write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        ensure_valid_spec_schema(document, path, allow_lazarus=True)
        written.append(
            WrittenLuantiSpec(
                task_id=task_id,
                change_number=binding.change_number,
                path=path,
                source=binding.source,
                evidence_layout=binding.states.layout,
            )
        )
    payload = {
        "state": "passed",
        "complete": True,
        "spec_count": len(written),
        "live_replay_spec_count": sum(
            1 for item in written if item.source == LIVE_SOURCE
        ),
        "frozen_evidence_spec_count": sum(
            1 for item in written if item.source == FROZEN_SOURCE
        ),
        "checkpoint_strategy": CHECKPOINT_STRATEGY,
        "specs": [
            {
                "task_id": item.task_id,
                "change_number": item.change_number,
                "path": str(item.path.relative_to(repo)),
                "observation_source": item.source,
                "evidence_layout": item.evidence_layout,
            }
            for item in written
        ],
        "schema_validated": True,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_SPECS.artifact_id,
        artifact_kind=_SPECS.artifact_kind,
        semantic_version=_SPECS.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_SPECS.artifact_id,
        artifact_kind=_SPECS.artifact_kind,
        semantic_version=_SPECS.semantic_version,
    )


def verify_luanti_task_specs(
    artifact_root: str | Path,
    *,
    repository_root: str | Path,
    authorization: AuthorizationRecord,
    task_ids: tuple[str, ...] | None = None,
) -> StableArtifact:
    """Resolve every written spec's checks against a fresh observation."""

    _require_scope(authorization, "preannotation-execution")
    root = Path(artifact_root).absolute().resolve(strict=True)
    repo = Path(repository_root).absolute().resolve(strict=True)
    bindings = build_luanti_bindings(root, task_ids=luanti_task_ids())
    results: list[dict[str, Any]] = []
    for task_id, binding in sorted(bindings.items()):
        if task_ids is not None and task_id not in task_ids:
            continue
        path = repo / "specs/luanti/lazarus" / f"{task_id}.yaml"
        lazarus = yaml.safe_load(path.read_text(encoding="utf-8"))["evaluation"][
            "lazarus"
        ]
        row: dict[str, Any] = {
            "task_id": task_id,
            "change_number": binding.change_number,
            "spec_path": str(path.relative_to(repo)),
            "observation_source": binding.source,
            "versions": {},
        }
        for version in ("fixed", "buggy"):
            audit = run_luanti_lazarus_probe(
                root, task_id=task_id, version=version, bindings=bindings
            )
            verdict = evaluate_luanti_lazarus_audit(audit)
            goal_ok, goal_failures = evaluate_luanti_lazarus_checks(
                lazarus["goal_checks"], audit
            )
            defect_ok, defect_failures = evaluate_luanti_lazarus_checks(
                lazarus["defect_signature"], audit
            )
            invariant_ok, invariant_failures = evaluate_luanti_lazarus_checks(
                lazarus["invariant_checks"], audit
            )
            row["versions"][version] = {
                "audit_complete": verdict.complete,
                "audit_failures": list(verdict.failures),
                "goal_checks_ok": goal_ok,
                "goal_failures": list(goal_failures),
                "defect_signature_ok": defect_ok,
                "defect_failures": list(defect_failures),
                "invariant_checks_ok": invariant_ok,
                "invariant_failures": list(invariant_failures),
                "probe_errors": list(audit.get("probe_errors") or []),
            }
        fixed = row["versions"]["fixed"]
        buggy = row["versions"]["buggy"]
        row["separates_goal_from_defect"] = bool(
            fixed["audit_complete"]
            and buggy["audit_complete"]
            and fixed["goal_checks_ok"]
            and not fixed["defect_signature_ok"]
            and buggy["defect_signature_ok"]
            and not buggy["goal_checks_ok"]
            and fixed["invariant_checks_ok"]
            and buggy["invariant_checks_ok"]
        )
        results.append(row)
    passed = bool(results) and all(row["separates_goal_from_defect"] for row in results)
    payload = {
        "state": "passed" if passed else "blocked",
        "complete": True,
        "verified_task_count": len(results),
        "all_specs_separate_goal_from_defect": passed,
        "live_replay_task_count": sum(
            1 for row in results if row["observation_source"] == LIVE_SOURCE
        ),
        "provider_calls_performed": False,
        "tasks": results,
        "paid_actions_authorized": False,
        "formal_trials_authorized": False,
    }
    write_stable_artifact(
        root,
        artifact_id=_VERIFICATION.artifact_id,
        artifact_kind=_VERIFICATION.artifact_kind,
        semantic_version=_VERIFICATION.semantic_version,
        created_at=_now(),
        inputs=(),
        payload=payload,
        authorization=authorization,
    )
    return load_stable_artifact(
        root,
        artifact_id=_VERIFICATION.artifact_id,
        artifact_kind=_VERIFICATION.artifact_kind,
        semantic_version=_VERIFICATION.semantic_version,
    )


def _spec_document(
    *, binding: LuantiProbeBinding, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Assemble one spec from the frozen manifest and the recovered state pair."""

    evaluation = manifest.get("evaluation") or {}
    visual_oracle = evaluation.get("visual_oracle")
    manifest_lazarus = evaluation.get("lazarus") or {}
    goldens = manifest_lazarus.get("goldens")
    if not isinstance(visual_oracle, Mapping) or not isinstance(goldens, Mapping):
        raise ValueError(f"{binding.task_id} manifest carries no oracle or goldens")
    candidate = manifest.get("candidate") or {}
    states = binding.states
    return {
        "name": f"C1-Lazarus Luanti {binding.task_id.removeprefix('lz-luanti-')}",
        "benchmark": "luanti",
        "timeout_ms": _TIMEOUT_MS,
        "tags": list(_TAGS),
        "setup": {
            "luanti_lazarus_probe": {
                "task_id": binding.task_id,
                "change_number": binding.change_number,
                "issue_number": candidate.get("issue_number"),
                "adapter": binding.adapter,
                "observation_source": binding.source,
                "registered_replay": list(binding.replay_steps),
                "evidence_layout": states.layout,
                "evidence_artifact_ids": list(states.source_artifact_ids),
                "formal_freeze_admission": states.admission_id,
                "upstream_truth_table": luanti_truth_table(),
            }
        },
        "target": {
            "kind": "synthesized_world",
            "candidate_versions": ["fixed", "buggy"],
            "game": "devtest",
            "fixture": "registered_candidate_worldmod",
        },
        "checkpoint": {
            "allowed_strategies": [CHECKPOINT_STRATEGY],
            "expected_validity": "save-restorable",
        },
        "actions": [],
        "evaluation": {
            "schema_version": 1,
            "semantic": {
                "assertions": [],
                "surfaces": {
                    surface: {
                        "adapter": binding.adapter,
                        "start": {"validate_target": True, "checks": []},
                        "goal": {"use_assertions": True, "checks": []},
                    }
                    for surface in ("gui", "state")
                },
            },
            "visual_oracle": _plain(visual_oracle),
            "lazarus": {
                "schema_version": 1,
                "goal_checks": luanti_lazarus_goal_checks(binding),
                "defect_signature": luanti_lazarus_defect_signature(binding),
                "invariant_checks": [{"type": PROBE_ERROR_CHECK}],
                "upstream_tests": {
                    "fail_to_pass": list(states.fail_to_pass),
                    "pass_to_pass": list(states.pass_to_pass),
                    "per_trial_p2p": list(states.pass_to_pass),
                },
                "goldens": _plain(goldens),
            },
        },
    }


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _require_scope(authorization: AuthorizationRecord, scope: str) -> None:
    if (
        type(authorization) is not AuthorizationRecord
        or authorization.scope != scope
        or authorization.revoked
        or authorization.local_user_id != os.getuid()
    ):
        raise PermissionError(f"an active {scope} authorization is required")


def _now() -> str:
    return (
        (datetime.now(UTC) + timedelta(microseconds=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


__all__ = [
    "WrittenLuantiSpec",
    "luanti_task_ids",
    "verify_luanti_task_specs",
    "write_luanti_task_specs",
]
